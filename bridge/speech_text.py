"""Simplified-Chinese speech output and conservative technical-term spelling."""
from functools import lru_cache
import re

# Domain context, not an expected answer. Never rewrite Chinese homophones into commands.
ASR_PROMPT = '以下是简体中文的软件开发语音，可能夹杂英文技术词。术语：项目目录、文件夹、启动程序、Python、Codex、USB、蓝牙、API。'
ASR_HOTWORDS = '项目目录 文件夹 启动程序 Python Codex USB API'


@lru_cache(maxsize=1)
def converter():
    from opencc import OpenCC
    return OpenCC('t2s')


def simplify(text):
    # Conversion failure must not silently fall back to Traditional Chinese.
    return converter().convert(text).strip()


def normalize_recognition(text):
    text = simplify(text)
    for term in ('Python', 'Codex', 'USB', 'API'):
        # Preserve case-sensitive filenames, paths and identifiers. Only normalize free words.
        text = re.sub(r'(?<![A-Za-z0-9_./\\-])'+term+r'(?![A-Za-z0-9_./\\-])', term,
                      text, flags=re.IGNORECASE)
    return text
