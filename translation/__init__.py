"""Python translation engine port of SakiRinn/LiveCaptions-Translator.

Reference: upstream master commit a6fee12757b15edbeef7c60f8895e0be694801e1.
ASR remains separate.
"""
from .api import TranslationAPI, TranslationSettings, google_translate
from .engine import GoogleTranslateSink
from .history import HistoryStore, TranslationEntry
__all__ = ["TranslationAPI","TranslationSettings","google_translate",
           "GoogleTranslateSink","HistoryStore","TranslationEntry"]
