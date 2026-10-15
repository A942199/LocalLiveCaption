"""Translation-only Python port of upstream TextUtil.cs and RegexPatterns.cs."""
import re

PUNC_EOS = ".?!。？！"
PUNC_COMMA = ",，、—\n"
SHORT_THRESHOLD = 10
MEDIUM_THRESHOLD = 40
VERYLONG_THRESHOLD = 220
SIM_THRESHOLD = 0.6
NOTICE = re.compile(r"^\[.+\] ")
THINK = re.compile(r"<think>.*?</think>")


def cj(ch):
    n = ord(ch)
    return any(a <= n <= b for a,b in ((0x4E00,0x9FFF),(0x3400,0x4DBF),
        (0x3000,0x303F),(0x3040,0x309F),(0x30A0,0x30FF),
        (0x31F0,0x31FF),(0x3200,0x32FF),(0x3300,0x33FF)))


def replace_newlines(text, threshold=MEDIUM_THRESHOLD):
    parts = text.split("\n")
    for i in range(len(parts)-1):
        part = parts[i].strip()
        if part:
            part += ("。" if cj(part[-1]) else ". ") if len(part.encode("utf-8")) >= threshold else ("——" if cj(part[-1]) else "—")
        parts[i] = part
    parts[-1] = parts[-1].strip()
    return "".join(parts)


def preprocess(text):
    # Translator.SyncLoop upstream ordering.
    text = re.sub(r"([A-Z])\s*\.\s*([A-Z])(?![A-Za-z]+)",r"\1\2",text)
    text = re.sub(r"([A-Z])\s*\.\s*([A-Z])(?=[A-Za-z]+)",r"\1 \2",text)
    text = re.sub(r"\s*([.!?,])\s*",r"\1 ",text)
    text = re.sub(r"\s*([。！？，、])\s*",r"\1",text)
    return replace_newlines(text)


def last_eos(text):
    return max((text.rfind(c) for c in PUNC_EOS),default=-1)


def latest_caption(text):
    if not text:
        return ""
    eos = last_eos(text[:-1] if text[-1] in PUNC_EOS else text)
    latest = text[eos+1:]
    if eos > 0 and len(latest.encode("utf-8")) < SHORT_THRESHOLD:
        latest = text[last_eos(text[:eos])+1:]
    return latest


def extract_original_caption(text):
    latest = latest_caption(text)
    eos = last_eos(latest)
    return latest[:eos+1] if eos >= 0 else latest


def shorten(text, maximum=VERYLONG_THRESHOLD):
    while len(text.encode("utf-8")) >= maximum:
        i = next((i for i,c in enumerate(text) if c in PUNC_EOS + PUNC_COMMA),-1)
        if i < 0 or i+1 >= len(text):
            break
        text = text[i+1:]
    return text


def _utf16_units(text):
    """C# string.Length/IndexOf use UTF-16 code units, unlike Python len()."""
    raw=text.encode("utf-16-le","surrogatepass")
    return [raw[i]|(raw[i+1]<<8) for i in range(0,len(raw),2)]


def _similarity_units(a,b):
    if a[:len(b)]==b or b[:len(a)]==a:
        return 1.0
    if len(a)>len(b):
        a,b=b,a
    old=list(range(len(a)+1))
    for j,right in enumerate(b,1):
        cur=[j]
        for i,left in enumerate(a,1):
            cur.append(min(cur[-1]+1,old[i]+1,old[i-1]+(left!=right)))
        old=cur
    return 1.0-old[-1]/max(1,len(b))


def similarity(a,b):
    return _similarity_units(_utf16_units(a),_utf16_units(b))


def similar_to_last(a,b):
    if not b:
        return False
    left,right=_utf16_units(a),_utf16_units(b)
    n=min(len(left),len(right))
    return _similarity_units(left[:n],right[:n])>SIM_THRESHOLD

