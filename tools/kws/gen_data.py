#!/usr/bin/env python3
"""Synthesize the KWS training corpus with edge-tts.

Positives: many zh voices x prosody variants saying 你好，openvela
Negatives: everyday sentences plus hard negatives (你好 / 你好啊 / 维拉 ...)

Output: data/pos_raw/*.wav, data/neg_raw/*.wav (16 kHz mono s16) and
data/manifest.json.  Re-running skips clips that already exist, so an
interrupted run (network hiccups) just resumes.
"""

import asyncio
import io
import json
import sys
from pathlib import Path

import av
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

import edge_tts

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SR = 16000

POS_TEXTS = [
    "你好，openvela。",
    "你好 openvela",
    "你好，open vela。",
    "你好！OpenVela。",
]

# round-7: how Mandarin speakers actually say the brand word.  A zh voice
# reads Latin "openvela" with English letter-to-sound rules (/v/, reduced
# vowels, stress-timed); a person says 欧朋维拉 — no /v/, four even
# syllables — or a mix.  Kept in a separate list so the draw sequence (and
# so the file names) of the original corpus above is untouched.
POS_TEXTS_EXTRA = [
    "你好，欧朋维拉。",
    "你好，欧喷维拉。",
    "你好，欧盆维拉。",
    "你好，欧本维拉。",
    "你好，open维拉。",
    "你好，噢喷薇拉。",
    "你好。欧朋维拉。",       # full stop: the long pause a hesitant user makes
]

# Voices: mainland + regional + TW/HK for accent diversity
VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunjianNeural",
    "zh-CN-YunxiNeural", "zh-CN-YunxiaNeural", "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-HsiaoYuNeural", "zh-TW-YunJheNeural",
    "zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
]

RATES = ["-20%", "-10%", "+0%", "+10%", "+25%"]
PITCHES = ["-40Hz", "+0Hz", "+40Hz"]

# Hard negatives first: things that share sounds with the wake phrase but
# must NOT fire.  ("hello openvela" is deliberately absent from BOTH sets:
# we neither train it to fire nor to be rejected.)
NEG_TEXTS = [
    "你好。", "你好啊。", "你好吗？", "你好呀。", "大家好。", "您好。",
    "你好，请问几点了？", "你好，帮我开灯。", "你好，小维。", "你好，薇拉。",
    "维拉。", "欧维拉。", "你好，维拉。", "欧朋，维拉。", "开的什么？",
    "open the door please.", "vanilla ice cream.", "propeller blades.",
    "凡是过往，皆为序章。", "今天天气怎么样？", "现在气温二十三度。",
    "明天可能会下雨，记得带伞。", "帮我设置一个八点的闹钟。",
    "把客厅的灯打开。", "把空调调到二十六度。", "播放一首轻音乐。",
    "这道菜需要先把油烧热。", "高铁比飞机更准时一些。", "会议改到下午三点。",
    "快递已经放在门口了。", "这个周末我们去爬山吧。", "小猫在沙发上睡着了。",
    "股市今天涨了百分之二。", "请把文件发到我的邮箱。", "楼下的便利店还开着。",
    "一二三四五六七八九十。", "二零二六年八月十四号。", "电话号码是一三八。",
    "水开了记得关火。", "地铁口右转直走两百米。", "这本书讲的是宇宙起源。",
    "他昨天刚从上海回来。", "衣服还在洗衣机里。", "记得给花浇水。",
    "电池还剩百分之三十。", "网络好像断了。", "声音再大一点。",
    "声音小一点。", "暂停播放。", "继续播放。", "下一首歌。",
    "太阳从东边升起。", "冰箱里还有两个苹果。", "钥匙放在玄关柜上。",
    "会议纪要我整理好了。", "这个方案还需要再讨论。", "代码已经提交了。",
    "服务器晚上要升级维护。", "编译大概需要十分钟。", "测试全部通过了。",
    "What time is it now?", "The weather is nice today.",
    "Please turn on the light.", "I will call you back later.",
    "The meeting starts at three.", "Happy birthday to you.",
    # round-5: extra generic material — streaming FAs spread beyond the
    # 你好-prefixed block, so widen everyday coverage
    "早上好，今天有三个会议。", "晚饭想吃什么？", "这个问题我再想想。",
    "帮我查一下明天的航班。", "空气质量还不错。", "记得带上充电器。",
    "这条路修了三个月了。", "咖啡还是茶？", "文件我已经打印好了。",
    "电梯在维修，走楼梯吧。", "上个月的报表发我一下。", "键盘该换电池了。",
    "窗户关一下，起风了。", "这个苹果有点酸。", "快点，要迟到了。",
    "慢一点说，我记一下。", "医生说多喝水多休息。", "车停在地下二层。",
    "密码我发你微信了。", "投影仪连不上了。", "外卖到楼下了。",
    "这首歌是谁唱的？", "周五下午做代码评审。", "打印机没纸了。",
    "空调温度调低两度。", "会议室换到三零二。", "网速今天特别慢。",
    "你说什么我没听清。", "现在开始播报新闻。", "欢迎收听今天的节目。",
    # round-7: real-world confusables — other assistants' wake words, 你好
    # openers (real speech has lots of 你好你好), and the brand word
    # without 你好 (must not fire on its own).  Appended at the END so the
    # per-sentence draws above keep their file names.
    "你好，小爱同学。", "小爱同学。", "小度小度。", "天猫精灵。", "你好，小艺。",
    "嗨，小问。", "你好，问问。", "你好，米雅。", "小布小布。", "你好你好。",
    "你好，请问一下。", "你好，我想问一下。", "喂，你好。", "你好呀，在吗？",
    "欧朋维拉。", "欧喷维拉。", "你好，欧朋。",
]

# Kept in the list (indices are baked into file names) but excluded from
# the manifest: a slow "你好，欧朋维拉" runs past the 2.0 s window, so the
# streaming positive IS the head-clipped "…欧朋维拉" — labeling the bare
# brand word negative would fight that.  Nobody says it without 你好.
NEG_SKIP = {"欧朋维拉。", "欧喷维拉。"}

CONCURRENCY = 6


async def tts_one(text, voice, rate, pitch, out_wav, sem):
    async with sem:
        for attempt in range(3):
            try:
                com = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                mp3 = io.BytesIO()
                async for chunk in com.stream():
                    if chunk["type"] == "audio":
                        mp3.write(chunk["data"])
                if mp3.tell() < 1024:
                    raise RuntimeError("empty audio")
                pcm = decode_mp3(mp3.getvalue())
                sf.write(out_wav, pcm, SR, subtype="PCM_16")
                return True
            except Exception as e:
                if attempt == 2:
                    print(f"FAIL {out_wav.name}: {e}", file=sys.stderr)
                    return False
                await asyncio.sleep(2.0 * (attempt + 1))


def decode_mp3(data):
    """mp3 bytes -> mono float32 @16k in [-1,1]"""
    with av.open(io.BytesIO(data)) as c:
        stream = c.streams.audio[0]
        chunks = []
        for frame in c.decode(stream):
            arr = frame.to_ndarray()          # (channels, n) or (1, n)
            if arr.dtype == np.int16:
                arr = arr.astype(np.float32) / 32768.0
            elif arr.dtype == np.int32:
                arr = arr.astype(np.float32) / 2147483648.0
            else:
                arr = arr.astype(np.float32)
            chunks.append(arr.mean(axis=0) if arr.ndim > 1 else arr)
        pcm = np.concatenate(chunks)
        rate = stream.rate
    if rate != SR:
        from math import gcd
        g = gcd(rate, SR)
        pcm = resample_poly(pcm, SR // g, rate // g)
    peak = np.abs(pcm).max()
    if peak > 1e-4:
        pcm = pcm * (0.7 / max(peak, 0.7))    # only attenuate clipping-close
    return pcm.astype(np.float32)


async def main():
    (DATA / "pos_raw").mkdir(parents=True, exist_ok=True)
    (DATA / "neg_raw").mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    rng = np.random.default_rng(20260814)
    jobs = []
    manifest = []

    # Positives: every voice x every text, with a sampled prosody grid
    for vi, voice in enumerate(VOICES):
        for ti, text in enumerate(POS_TEXTS):
            picks = {(rng.integers(len(RATES)), rng.integers(len(PITCHES)))
                     for _ in range(6)}
            picks.add((2, 1))                 # always include neutral
            for ri, pi in picks:
                name = f"pos_v{vi:02d}_t{ti}_r{ri}_p{pi}.wav"
                out = DATA / "pos_raw" / name
                manifest.append({"file": f"pos_raw/{name}", "label": 1,
                                 "voice": voice, "text": text})
                if not out.exists():
                    jobs.append(tts_one(text, voice, RATES[ri], PITCHES[pi],
                                        out, sem))

    # Negatives: every sentence by 8 voices — streaming false alarms were
    # not confined to the 你好-prefixed block, so give the whole negative
    # set full voice coverage
    for si, text in enumerate(NEG_TEXTS):
        nv = 8
        vs = rng.choice(len(VOICES), size=nv, replace=False)
        for vi in vs:
            ri = int(rng.integers(1, 4))
            name = f"neg_s{si:03d}_v{vi:02d}.wav"
            out = DATA / "neg_raw" / name
            if text in NEG_SKIP:
                continue
            manifest.append({"file": f"neg_raw/{name}", "label": 0,
                             "voice": VOICES[vi], "text": text})
            if not out.exists():
                jobs.append(tts_one(text, VOICES[vi], RATES[ri], "+0Hz",
                                    out, sem))

    # round-7 positives (Mandarin renderings): own RNG so nothing above
    # changes; 3 random prosody picks + neutral per voice x text
    rng2 = np.random.default_rng(20260829)
    for vi, voice in enumerate(VOICES):
        for tj, text in enumerate(POS_TEXTS_EXTRA):
            ti = len(POS_TEXTS) + tj
            picks = {(rng2.integers(len(RATES)), rng2.integers(len(PITCHES)))
                     for _ in range(3)}
            picks.add((2, 1))
            for ri, pi in picks:
                name = f"pos_v{vi:02d}_t{ti}_r{ri}_p{pi}.wav"
                out = DATA / "pos_raw" / name
                manifest.append({"file": f"pos_raw/{name}", "label": 1,
                                 "voice": voice, "text": text})
                if not out.exists():
                    jobs.append(tts_one(text, voice, RATES[ri], PITCHES[pi],
                                        out, sem))

    print(f"{len(jobs)} clips to synthesize "
          f"({len(manifest)} total in manifest)")
    if "--dry" in sys.argv:            # just report; keep manifest.json
        return
    results = await asyncio.gather(*jobs)
    ok = sum(1 for r in results if r)
    print(f"done: {ok}/{len(jobs)} new clips OK")

    # keep only entries whose file actually exists
    manifest = [m for m in manifest if (DATA / m["file"]).exists()]
    (DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    npos = sum(1 for m in manifest if m["label"] == 1)
    print(f"manifest: {npos} positives, {len(manifest) - npos} negatives")


if __name__ == "__main__":
    asyncio.run(main())
