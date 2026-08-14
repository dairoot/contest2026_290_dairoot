// SPDX-License-Identifier: GPL-2.0
/*
 * snd_rpmsg_mic.c - ALSA capture card fed by the openvela AMP slave.
 *
 * The microphone hangs off PDM1 (or SAI2), which belongs to openvela running
 * on cpu3.  openvela captures the PCM and pushes it over rpmsg under the
 * protocol in rpmsg_mic_proto.h; this driver turns that stream into a normal
 * ALSA capture device, so the AMP microphone records like any other:
 *
 *   arecord -D hw:rpmsgmic,0 -f S16_LE -r 16000 -c 1 -d 5 mic.wav
 *
 * Data arrives as unsolicited RSP_DATA messages in rpmsg receive context; we
 * copy each chunk into the ALSA ring buffer and report a period whenever one
 * is complete.  There is no DMA and no hardware pointer: the "hardware"
 * position is simply how much the remote core has handed us.
 *
 * Copyright (c) 2026
 */

#include <linux/completion.h>
#include <linux/input.h>
#include <linux/module.h>
#include <linux/rpmsg.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

#include <sound/control.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/pcm.h>

#include "rpmsg_mic_proto.h"

#define DRV_NAME        "snd_rpmsg_mic"
#define CARD_ID         "rpmsgmic"

#define CAPS_TIMEOUT_MS 2000
#define BUFFER_BYTES_MAX (256 * 1024)

static bool xrun_on_drop;
module_param(xrun_on_drop, bool, 0644);
MODULE_PARM_DESC(xrun_on_drop,
		 "Stop the stream with -EPIPE when the slave drops PCM "
		 "(default: keep going and only count the loss)");

struct rpmsg_mic {
	struct rpmsg_device *rpdev;
	struct snd_card *card;
	struct snd_pcm *pcm;

	struct completion caps_done;
	struct rpmsg_mic_caps caps;

	/* Serialises the stream state below against the rpmsg callback. */
	spinlock_t lock;

	struct snd_pcm_substream *substream;
	bool running;
	unsigned int buf_pos;		/* bytes into the ALSA ring buffer */
	unsigned int period_acc;	/* bytes since the last period report */
	u32 next_seq;			/* sequence we expect next */
	u32 last_dropped;		/* slave's drop counter, last seen */
	u32 lost_chunks;		/* chunks we know went missing */

	/* trigger() runs atomically, but rpmsg_send() may sleep. */
	struct work_struct cmd_work;
	u16 pending_cmd;

	unsigned int chan;		/* PDM data slot, 0 or 1 */

	/* Wake-word events from the slave's on-core KWS engine. */
	struct input_dev *input;
	u32 wake_count;
};

/*
 * Command path
 */

static int rpmsg_mic_send_cmd(struct rpmsg_mic *mic, u16 type, u16 arg)
{
	struct rpmsg_mic_hdr hdr = {
		.magic = RPMSG_MIC_MAGIC,
		.type = type,
		.arg = arg,
	};

	return rpmsg_send(mic->rpdev->ept, &hdr, sizeof(hdr));
}

static void rpmsg_mic_cmd_work(struct work_struct *work)
{
	struct rpmsg_mic *mic = container_of(work, struct rpmsg_mic, cmd_work);
	unsigned long flags;
	u16 cmd;

	spin_lock_irqsave(&mic->lock, flags);
	cmd = mic->pending_cmd;
	mic->pending_cmd = 0;
	spin_unlock_irqrestore(&mic->lock, flags);

	if (cmd)
		rpmsg_mic_send_cmd(mic, cmd, 0);
}

/*
 * Receive path
 */

static void rpmsg_mic_push(struct rpmsg_mic *mic,
			   const struct rpmsg_mic_hdr *hdr,
			   const void *pcm, unsigned int bytes)
{
	struct snd_pcm_substream *substream;
	struct snd_pcm_runtime *runtime;
	unsigned int buf_bytes, period_bytes, first;
	bool elapsed = false, lost = false;
	unsigned long flags;

	spin_lock_irqsave(&mic->lock, flags);

	substream = mic->substream;
	if (!mic->running || !substream || !substream->runtime) {
		spin_unlock_irqrestore(&mic->lock, flags);
		return;
	}

	runtime = substream->runtime;
	buf_bytes = snd_pcm_lib_buffer_bytes(substream);
	period_bytes = snd_pcm_lib_period_bytes(substream);

	/*
	 * Two independent ways to lose audio: rpmsg dropped a packet (seq
	 * jumps) or the slave's ring overflowed because we were too slow
	 * (its drop counter moves).  Either way the timeline has a hole.
	 */
	if (hdr->seq != mic->next_seq || hdr->dropped != mic->last_dropped)
		lost = true;

	mic->next_seq = hdr->seq + 1;
	mic->last_dropped = hdr->dropped;
	if (lost)
		mic->lost_chunks++;

	if (bytes > buf_bytes)
		bytes = buf_bytes;

	first = min(bytes, buf_bytes - mic->buf_pos);
	memcpy(runtime->dma_area + mic->buf_pos, pcm, first);
	if (bytes > first)
		memcpy(runtime->dma_area, pcm + first, bytes - first);

	mic->buf_pos = (mic->buf_pos + bytes) % buf_bytes;

	mic->period_acc += bytes;
	if (mic->period_acc >= period_bytes) {
		mic->period_acc -= period_bytes;
		elapsed = true;
	}

	spin_unlock_irqrestore(&mic->lock, flags);

	if (lost) {
		dev_warn_ratelimited(&mic->rpdev->dev,
				     "PCM gap: seq %u, slave drops %u\n",
				     hdr->seq, hdr->dropped);
		if (xrun_on_drop) {
			snd_pcm_stop_xrun(substream);
			return;
		}
	}

	if (elapsed)
		snd_pcm_period_elapsed(substream);
}

static int rpmsg_mic_cb(struct rpmsg_device *rpdev, void *data, int len,
			void *priv, u32 src)
{
	struct rpmsg_mic *mic = dev_get_drvdata(&rpdev->dev);
	const struct rpmsg_mic_hdr *hdr = data;

	if (!mic || len < (int)sizeof(*hdr) || hdr->magic != RPMSG_MIC_MAGIC)
		return 0;

	switch (hdr->type) {
	case RPMSG_MIC_RSP_CAPS:
		if (len >= (int)(sizeof(*hdr) + sizeof(mic->caps))) {
			memcpy(&mic->caps, (const u8 *)data + sizeof(*hdr),
			       sizeof(mic->caps));
			complete(&mic->caps_done);
		}
		break;

	case RPMSG_MIC_RSP_DATA:
		rpmsg_mic_push(mic, hdr, (const u8 *)data + sizeof(*hdr),
			       len - sizeof(*hdr));
		break;

	case RPMSG_MIC_EVT_WAKE:
		/*
		 * The slave's offline wake-word engine heard the phrase.
		 * Surface it as a KEY_WAKEUP press so userspace can react
		 * with plain input APIs (evtest, libinput, ...).
		 */
		mic->wake_count++;
		dev_info(&rpdev->dev,
			 "wake word detected: p=0.%03u #%u (slave ts %llu us)\n",
			 hdr->arg, hdr->seq, hdr->ts_us);
		if (mic->input) {
			input_report_key(mic->input, KEY_WAKEUP, 1);
			input_sync(mic->input);
			input_report_key(mic->input, KEY_WAKEUP, 0);
			input_sync(mic->input);
		}
		break;

	default:
		break;
	}

	return 0;
}

/*
 * PCM ops
 */

static const struct snd_pcm_hardware rpmsg_mic_pcm_hw = {
	.info = SNDRV_PCM_INFO_INTERLEAVED |
		SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_MMAP |
		SNDRV_PCM_INFO_MMAP_VALID,
	.formats = SNDRV_PCM_FMTBIT_S16_LE,
	.rates = SNDRV_PCM_RATE_KNOT,
	.channels_min = 1,
	.channels_max = 1,
	.buffer_bytes_max = BUFFER_BYTES_MAX,
	.period_bytes_min = RPMSG_MIC_CHUNK_BYTES,
	.period_bytes_max = BUFFER_BYTES_MAX / 2,
	.periods_min = 2,
	.periods_max = 64,
};

static int rpmsg_mic_open(struct snd_pcm_substream *substream)
{
	struct rpmsg_mic *mic = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;
	int ret;

	runtime->hw = rpmsg_mic_pcm_hw;
	runtime->hw.rate_min = mic->caps.rate;
	runtime->hw.rate_max = mic->caps.rate;
	runtime->hw.channels_min = mic->caps.channels;
	runtime->hw.channels_max = mic->caps.channels;

	/*
	 * The slave clocks itself; it cannot be retuned, so the rate is a
	 * single value rather than a range.
	 */
	ret = snd_pcm_hw_constraint_single(runtime, SNDRV_PCM_HW_PARAM_RATE,
					   mic->caps.rate);
	if (ret < 0)
		return ret;

	/* Whole chunks only, so a period boundary cannot split one. */
	ret = snd_pcm_hw_constraint_step(runtime, 0,
					 SNDRV_PCM_HW_PARAM_PERIOD_BYTES,
					 RPMSG_MIC_CHUNK_BYTES);
	if (ret < 0)
		return ret;

	return 0;
}

static int rpmsg_mic_close(struct snd_pcm_substream *substream)
{
	struct rpmsg_mic *mic = snd_pcm_substream_chip(substream);
	unsigned long flags;

	spin_lock_irqsave(&mic->lock, flags);
	mic->running = false;
	mic->substream = NULL;
	spin_unlock_irqrestore(&mic->lock, flags);

	cancel_work_sync(&mic->cmd_work);
	rpmsg_mic_send_cmd(mic, RPMSG_MIC_CMD_STOP, 0);

	return 0;
}

static int rpmsg_mic_prepare(struct snd_pcm_substream *substream)
{
	struct rpmsg_mic *mic = snd_pcm_substream_chip(substream);
	unsigned long flags;

	spin_lock_irqsave(&mic->lock, flags);
	mic->substream = substream;
	mic->buf_pos = 0;
	mic->period_acc = 0;
	mic->next_seq = 0;
	mic->last_dropped = mic->caps.overruns ? mic->last_dropped : 0;
	spin_unlock_irqrestore(&mic->lock, flags);

	return 0;
}

static int rpmsg_mic_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct rpmsg_mic *mic = snd_pcm_substream_chip(substream);

	/* Called with a spinlock held: hand the rpmsg_send() to a worker. */

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		mic->substream = substream;
		mic->next_seq = 0;
		mic->running = true;
		mic->pending_cmd = RPMSG_MIC_CMD_START;
		break;

	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
		mic->running = false;
		mic->pending_cmd = RPMSG_MIC_CMD_STOP;
		break;

	default:
		return -EINVAL;
	}

	queue_work(system_wq, &mic->cmd_work);

	return 0;
}

static snd_pcm_uframes_t rpmsg_mic_pointer(struct snd_pcm_substream *substream)
{
	struct rpmsg_mic *mic = snd_pcm_substream_chip(substream);
	unsigned long flags;
	unsigned int pos;

	spin_lock_irqsave(&mic->lock, flags);
	pos = mic->buf_pos;
	spin_unlock_irqrestore(&mic->lock, flags);

	return bytes_to_frames(substream->runtime, pos);
}

static const struct snd_pcm_ops rpmsg_mic_pcm_ops = {
	.open = rpmsg_mic_open,
	.close = rpmsg_mic_close,
	.prepare = rpmsg_mic_prepare,
	.trigger = rpmsg_mic_trigger,
	.pointer = rpmsg_mic_pointer,
};

/*
 * Mixer controls: the PDM data line carries two slots on the two clock
 * edges, and which one the microphone sits on depends on how its SELECT pin
 * is strapped.  Expose the choice instead of hard-coding it.
 */

static int rpmsg_mic_chan_info(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_info *uinfo)
{
	static const char * const names[] = { "Slot 0", "Slot 1" };

	return snd_ctl_enum_info(uinfo, 1, ARRAY_SIZE(names), names);
}

static int rpmsg_mic_chan_get(struct snd_kcontrol *kcontrol,
			      struct snd_ctl_elem_value *ucontrol)
{
	struct rpmsg_mic *mic = snd_kcontrol_chip(kcontrol);

	ucontrol->value.enumerated.item[0] = mic->chan;

	return 0;
}

static int rpmsg_mic_chan_put(struct snd_kcontrol *kcontrol,
			      struct snd_ctl_elem_value *ucontrol)
{
	struct rpmsg_mic *mic = snd_kcontrol_chip(kcontrol);
	unsigned int sel = ucontrol->value.enumerated.item[0];
	int ret;

	if (sel > 1)
		return -EINVAL;

	if (sel == mic->chan)
		return 0;

	ret = rpmsg_mic_send_cmd(mic, RPMSG_MIC_CMD_CHAN, sel);
	if (ret)
		return ret;

	mic->chan = sel;

	return 1;
}

static int rpmsg_mic_lost_info(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_info *uinfo)
{
	uinfo->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	uinfo->count = 1;
	uinfo->value.integer.min = 0;
	uinfo->value.integer.max = INT_MAX;

	return 0;
}

static int rpmsg_mic_lost_get(struct snd_kcontrol *kcontrol,
			      struct snd_ctl_elem_value *ucontrol)
{
	struct rpmsg_mic *mic = snd_kcontrol_chip(kcontrol);

	ucontrol->value.integer.value[0] = mic->lost_chunks;

	return 0;
}

static const struct snd_kcontrol_new rpmsg_mic_controls[] = {
	{
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.name = "PDM Data Slot",
		.access = SNDRV_CTL_ELEM_ACCESS_READWRITE,
		.info = rpmsg_mic_chan_info,
		.get = rpmsg_mic_chan_get,
		.put = rpmsg_mic_chan_put,
	},
	{
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.name = "Lost Chunks",
		.access = SNDRV_CTL_ELEM_ACCESS_READ |
			  SNDRV_CTL_ELEM_ACCESS_VOLATILE,
		.info = rpmsg_mic_lost_info,
		.get = rpmsg_mic_lost_get,
	},
};

/*
 * Probe / remove
 */

static int rpmsg_mic_probe(struct rpmsg_device *rpdev)
{
	struct rpmsg_mic *mic;
	struct snd_card *card;
	struct snd_pcm *pcm;
	unsigned int i;
	int ret;

	ret = snd_card_new(&rpdev->dev, SNDRV_DEFAULT_IDX1, CARD_ID,
			   THIS_MODULE, sizeof(*mic), &card);
	if (ret < 0)
		return ret;

	mic = card->private_data;
	mic->rpdev = rpdev;
	mic->card = card;
	spin_lock_init(&mic->lock);
	init_completion(&mic->caps_done);
	INIT_WORK(&mic->cmd_work, rpmsg_mic_cmd_work);
	dev_set_drvdata(&rpdev->dev, mic);

	/* Ask the slave what it has before advertising anything to ALSA. */

	ret = rpmsg_mic_send_cmd(mic, RPMSG_MIC_CMD_CAPS, 0);
	if (ret) {
		dev_err(&rpdev->dev, "caps request failed: %d\n", ret);
		goto err;
	}

	if (!wait_for_completion_timeout(&mic->caps_done,
					 msecs_to_jiffies(CAPS_TIMEOUT_MS))) {
		dev_err(&rpdev->dev, "no caps reply from the AMP slave\n");
		ret = -ETIMEDOUT;
		goto err;
	}

	if (!mic->caps.rate || mic->caps.bits != 16 || mic->caps.channels != 1) {
		dev_err(&rpdev->dev,
			"unusable caps: rate %u bits %u channels %u\n",
			mic->caps.rate, mic->caps.bits, mic->caps.channels);
		ret = -ENODEV;
		goto err;
	}

	ret = snd_pcm_new(card, "rpmsg-mic", 0, 0, 1, &pcm);
	if (ret < 0)
		goto err;

	pcm->private_data = mic;
	pcm->info_flags = 0;
	strscpy(pcm->name, "AMP microphone", sizeof(pcm->name));
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &rpmsg_mic_pcm_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL,
				       0, BUFFER_BYTES_MAX);
	mic->pcm = pcm;

	for (i = 0; i < ARRAY_SIZE(rpmsg_mic_controls); i++) {
		/* The slot control only means anything on a PDM front end. */
		if (i == 0 && !(mic->caps.flags & RPMSG_MIC_FLAG_PDM))
			continue;

		ret = snd_ctl_add(card,
				  snd_ctl_new1(&rpmsg_mic_controls[i], mic));
		if (ret < 0)
			goto err;
	}

	/*
	 * Wake-word event channel.  Optional: losing it degrades to dmesg
	 * lines only, so a failure here does not fail the probe.
	 */
	mic->input = devm_input_allocate_device(&rpdev->dev);
	if (mic->input) {
		mic->input->name = "openvela-kws";
		mic->input->phys = "rpmsg-mic/kws";
		mic->input->id.bustype = BUS_VIRTUAL;
		input_set_capability(mic->input, EV_KEY, KEY_WAKEUP);
		ret = input_register_device(mic->input);
		if (ret) {
			dev_warn(&rpdev->dev,
				 "kws input device failed: %d\n", ret);
			mic->input = NULL;
		}
	}

	strscpy(card->driver, DRV_NAME, sizeof(card->driver));
	strscpy(card->shortname, "openvela AMP mic", sizeof(card->shortname));
	snprintf(card->longname, sizeof(card->longname),
		 "openvela AMP microphone over rpmsg, %u Hz %u bit mono",
		 mic->caps.rate, mic->caps.bits);

	ret = snd_card_register(card);
	if (ret < 0)
		goto err;

	dev_info(&rpdev->dev,
		 "card %d: %u Hz, %u samples/chunk, slave overruns %u\n",
		 card->number, mic->caps.rate, mic->caps.chunk_samples,
		 mic->caps.overruns);

	return 0;

err:
	snd_card_free(card);

	return ret;
}

static void rpmsg_mic_remove(struct rpmsg_device *rpdev)
{
	struct rpmsg_mic *mic = dev_get_drvdata(&rpdev->dev);
	unsigned long flags;

	spin_lock_irqsave(&mic->lock, flags);
	mic->running = false;
	mic->substream = NULL;
	spin_unlock_irqrestore(&mic->lock, flags);

	cancel_work_sync(&mic->cmd_work);
	rpmsg_mic_send_cmd(mic, RPMSG_MIC_CMD_STOP, 0);

	snd_card_free(mic->card);
}

static struct rpmsg_device_id rpmsg_mic_id_table[] = {
	{ .name = RPMSG_MIC_EPT_NAME },
	{ },
};
MODULE_DEVICE_TABLE(rpmsg, rpmsg_mic_id_table);

static struct rpmsg_driver rpmsg_mic_driver = {
	.drv.name = DRV_NAME,
	.id_table = rpmsg_mic_id_table,
	.probe = rpmsg_mic_probe,
	.callback = rpmsg_mic_cb,
	.remove = rpmsg_mic_remove,
};
module_rpmsg_driver(rpmsg_mic_driver);

MODULE_DESCRIPTION("ALSA capture card for the openvela AMP microphone");
MODULE_LICENSE("GPL v2");
