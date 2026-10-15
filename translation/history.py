"""SQLite history and upstream-compatible search, pagination and import."""
import csv
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from .text import similar_to_last, NOTICE, PUNC_EOS, cj


@dataclass(frozen=True)
class TranslationEntry:
    timestamp: str
    source: str
    translated: str
    target: str
    engine: str


def _timestamp(value):
    """Migrate historical C# Unix timestamps and deprecated date strings."""
    raw = str(value)
    try:
        return datetime.fromtimestamp(float(raw)).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        try:
            return datetime.fromisoformat(raw).isoformat(timespec="seconds")
        except ValueError:
            return raw


class HistoryStore:
    def __init__(self,path):
        self.path=str(path)
        self.lock=threading.RLock()
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with self.lock,self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS translation_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                source_text TEXT NOT NULL,translated_text TEXT NOT NULL,
                target_language TEXT NOT NULL,api_used TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS translation_migrations(
                migration TEXT PRIMARY KEY)""")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='TranslationHistory'").fetchone():
                marker=db.execute("""SELECT 1 FROM translation_migrations
                                       WHERE migration='import_upstream'""").fetchone()
                if not marker:
                    # Never destroy the original upstream table or import twice.
                    for row in db.execute("""SELECT Timestamp,SourceText,TranslatedText,
                                              TargetLanguage,ApiUsed FROM TranslationHistory
                                              ORDER BY Id ASC""").fetchall():
                        db.execute("""INSERT INTO translation_history(
                            timestamp,source_text,translated_text,target_language,api_used
                            ) VALUES(?,?,?,?,?)""",(_timestamp(row[0]),*row[1:]))
                    db.execute("""INSERT INTO translation_migrations(migration)
                                  VALUES('import_upstream')""")

    @contextmanager
    def _db(self):
        connection=sqlite3.connect(self.path,timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _entry(row):
        return TranslationEntry(*row)

    def last(self):
        with self.lock,self._db() as db:
            row=db.execute("""SELECT timestamp,source_text,translated_text,
                 target_language,api_used FROM translation_history
                 ORDER BY id DESC LIMIT 1""").fetchone()
            return self._entry(row) if row else None

    def log(self,source,translated,target,engine,overwrite=None):
        with self.lock,self._db() as db:
            last=db.execute("""SELECT id,source_text FROM translation_history
                               ORDER BY id DESC LIMIT 1""").fetchone()
            if overwrite is None:
                overwrite=bool(last and similar_to_last(source,last[1]))
            if overwrite and last:
                db.execute("DELETE FROM translation_history WHERE id=?",(last[0],))
            db.execute("""INSERT INTO translation_history(
                    timestamp,source_text,translated_text,target_language,api_used)
                    VALUES(?,?,?,?,?)""",(datetime.now().isoformat(timespec="seconds"),
                    source,translated,target,engine))
            return bool(overwrite and last)

    def delete_last(self):
        with self.lock,self._db() as db:
            db.execute("""DELETE FROM translation_history WHERE id IN (
                          SELECT id FROM translation_history ORDER BY id DESC LIMIT 1)""")

    def recent(self,limit=10):
        with self.lock,self._db() as db:
            rows=db.execute("""SELECT timestamp,source_text,translated_text,
                target_language,api_used FROM translation_history ORDER BY id DESC LIMIT ?""",
                (max(0,min(100000,int(limit))),)).fetchall()
        return [self._entry(row) for row in reversed(rows)]

    def search(self,term="",page=1,page_size=50):
        """Return (newest-first rows, maximum page), matching upstream semantics."""
        page_size=max(1,int(page_size))
        page=max(1,int(page))
        term="%"+str(term)+"%"
        with self.lock,self._db() as db:
            count=db.execute("""SELECT COUNT(*) FROM translation_history
                WHERE source_text LIKE ? OR translated_text LIKE ?""",(term,term)).fetchone()[0]
            rows=db.execute("""SELECT timestamp,source_text,translated_text,
                target_language,api_used FROM translation_history
                WHERE source_text LIKE ? OR translated_text LIKE ?
                ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?""",
                (term,term,page_size,(page-1)*page_size)).fetchall()
        return [self._entry(row) for row in rows],max(1,(count+page_size-1)//page_size)

    def count(self,term=""):
        query="%"+str(term)+"%"
        with self.lock,self._db() as db:
            return int(db.execute("""SELECT COUNT(*) FROM translation_history
                   WHERE source_text LIKE ? OR translated_text LIKE ?""",
                   (query,query)).fetchone()[0])

    def clear(self):
        with self.lock,self._db() as db:
            db.execute("DELETE FROM translation_history")
            db.execute("DELETE FROM sqlite_sequence WHERE name='translation_history'")

    def export_csv(self,path):
        """Export ALL records, independent of GUI page size (UTF-8 BOM)."""
        with self.lock,self._db() as db:
            rows=db.execute("""SELECT timestamp,source_text,translated_text,
                target_language,api_used FROM translation_history
                ORDER BY timestamp DESC,id DESC""").fetchall()
        with open(path,"w",newline="",encoding="utf-8-sig") as fp:
            writer=csv.writer(fp)
            writer.writerow(("Timestamp","TimestampFull","SourceText",
                             "TranslatedText","TargetLanguage","ApiUsed"))
            for row in rows:
                writer.writerow((row[0][:16].replace("T"," "),row[0].replace("T"," "),
                                 *row[1:]))
        return len(rows)

    def contexts(self,limit=2):
        return [r for r in self.recent(min(10,limit))
                if r.translated!="N/A" and "[ERROR]" not in r.translated
                and "[WARNING]" not in r.translated]

    def previous_text(self,limit=1,translated=True,entries=None):
        pieces=[]
        for entry in (self.contexts(limit) if entries is None else entries[-limit:] if limit>0 else []):
            # Upstream Caption.GetPreviousText suppresses invalid log cards.
            if (entry.translated=="N/A" or "[ERROR]" in entry.translated
                    or "[WARNING]" in entry.translated):
                continue
            value=NOTICE.sub("",entry.translated) if translated else entry.source
            if not value:
                continue
            if pieces and pieces[-1][-1] not in PUNC_EOS:
                pieces[-1]+="。" if cj(pieces[-1][-1]) else ". "
            pieces.append(value)
        text="".join(pieces)
        if text and text[-1] not in PUNC_EOS:
            text+="。" if cj(text[-1]) else "."
        if text and ord(text[-1])<128:
            text+=" "
        return text

