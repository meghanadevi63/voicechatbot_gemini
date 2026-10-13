"""Prechecks for multilingual chat: English documents, answers in the language the user speaks.

Tries the proposed prompt changes (defined here, the backend is not modified) on Hindi and Telugu questions.

Checks (run all by default, or pick with --checks):
  text        typed path: current prompts vs proposed ones. Router query language, retrieval overlap with the
              English question's pages, answer language for Groq gpt-oss-20b and Gemini Flash Lite
  speech      make spoken Hindi/Telugu questions (Gemini Live reads each aloud), cached as 16 kHz WAV
  transcribe  dictation: current vs proposed transcription prompt, script and accuracy
  live        hands-free agent mode with the proposed prompt: transcript script, tool query language,
              answer language

Uses a copy of the Milvus Lite database, so the backend can keep running.
Writes WAV files and report.json to data/multilang_precheck/.

    python -m scripts.multilang_precheck
    python -m scripts.multilang_precheck --checks text --langs te
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import time
import traceback
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv

from scripts.live_spike import (
    IN_RATE,
    OUT_RATE,
    SEARCH_TOOL,
    TTS_PROMPT,
    collect_turn,
    latency,
    live_config,
    load_wav,
    resample,
    save_wav,
    stream_audio,
)

OUT_DIR = Path("data/multilang_precheck")

# English baseline and translations. Index 2 is code-mixed on purpose (English term in native script).
QUESTIONS = {
    "en": [
        "What is the habit loop?",
        "What are the four laws of behavior change?",
        "What is habit stacking?",
        "What is the two-minute rule?",
        "How does environment design affect our habits?",
    ],
    "hi": [
        "आदतों का चक्र क्या होता है?",
        "व्यवहार परिवर्तन के चार नियम क्या हैं?",
        "हैबिट स्टैकिंग क्या है?",
        "दो मिनट का नियम क्या है?",
        "माहौल हमारी आदतों पर कैसे असर डालता है?",
    ],
    "te": [
        "అలవాట్ల చక్రం అంటే ఏమిటి?",
        "ప్రవర్తన మార్పు యొక్క నాలుగు నియమాలు ఏమిటి?",
        "హ్యాబిట్ స్టాకింగ్ అంటే ఏమిటి?",
        "రెండు నిమిషాల నియమం ఏమిటి?",
        "మన చుట్టూ ఉన్న వాతావరణం మన అలవాట్లపై ఎలా ప్రభావం చూపుతుంది?",
    ],
}
SMALL_TALK = {"hi": "नमस्ते, आप कैसे हैं?", "te": "నమస్తే, మీరు ఎలా ఉన్నారు?"}
NOT_IN_DOCS = {"hi": "फ्रांस की राजधानी क्या है?", "te": "ఫ్రాన్స్ రాజధాని ఏమిటి?"}
FOLLOW_UP = {"hi": "इसके बारे में और बताइए", "te": "దీని గురించి ఇంకా చెప్పండి"}
SCRIPT_OF = {"hi": "devanagari", "te": "telugu", "en": "latin"}
LANG_NAME = {"hi": "Hindi", "te": "Telugu", "en": "English"}

# --- proposed prompt changes --------------------------------------------------------------------

ROUTER_RULE = """

Language: the user may write in any language, but the documents are in English. Always write `search_query`
in English, translating the user's message if needed and keeping its key terms. Write `reply` in the same
language as the user's latest message."""

ANSWER_RULE = """
- The excerpts are in English, but reply in the language of the user's latest message, in that language's
  usual script (for example Hindi in Devanagari, Telugu in Telugu script). If the message is too short to
  tell its language, use the language of the conversation so far. This includes saying you couldn't find
  something."""

AGENT_RULE = """
- The documents are in English: always call search_documents with an English query, translating the user's
  question if needed.
- Answer in the language the user is speaking, in that language's usual script."""

TRANSCRIBE_PROMPT_NEW = (
    "Transcribe the speech in this audio exactly as spoken, in the language it was spoken in and in that "
    "language's usual script (for example Hindi in Devanagari, Telugu in Telugu script). Do not translate. "
    "Reply with only the transcript. If there is no speech, reply with nothing."
)

# --- text helpers --------------------------------------------------------------------------------

SCRIPT_RANGES = {"devanagari": (0x0900, 0x097F), "telugu": (0x0C00, 0x0C7F)}


def script_mix(text: str) -> dict:
    """Share of letters per script, e.g. {"telugu": 0.93, "latin": 0.07}."""
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        name = next((n for n, (lo, hi) in SCRIPT_RANGES.items() if lo <= cp <= hi), None)
        name = name or ("latin" if cp < 0x250 else "other")
        counts[name] = counts.get(name, 0) + 1
    total = sum(counts.values()) or 1
    return {k: round(v / total, 2) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])}


def in_script(text: str, lang: str, threshold: float = 0.7) -> bool:
    return script_mix(text).get(SCRIPT_OF[lang], 0) >= threshold


def similarity(a: str, b: str) -> float:
    """Unicode-aware word match (the live.py version only sees a-z)."""
    words = lambda s: re.findall(r"\w+", s.lower())
    return round(SequenceMatcher(None, words(a), words(b)).ratio(), 3)


def pages(docs) -> list:
    return [d.metadata.get("page") for d in docs]


def overlap(a: list, b: list) -> float:
    return round(len(set(a) & set(b)) / max(len(set(b)), 1), 2)


def median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


# --- checks --------------------------------------------------------------------------------------


class Precheck:
    def __init__(self, args):
        from google import genai
        from google.genai import types

        self.args = args
        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.langs = args.langs
        self._rag = None

    @property
    def rag(self):
        if self._rag is None:
            from backend.rag import RAGChain

            self._rag = RAGChain()
        return self._rag

    def proposed_chains(self):
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_groq import ChatGroq

        from backend import rag
        from backend.config import get_settings

        s = get_settings()
        # M1 moved a language rule into SYSTEM_PROMPT; fill its slot generically so this precheck still runs
        base = rag.SYSTEM_PROMPT.replace("{reply_language}", "reply in the language of the user's latest message")
        answer_prompt = base.replace("\n\nDocument excerpts:", ANSWER_RULE + "\n\nDocument excerpts:")
        router = ChatPromptTemplate.from_messages(
            [("system", rag.ROUTER_PROMPT + ROUTER_RULE), MessagesPlaceholder("history"), ("human", "{question}")]
        ) | ChatGroq(model=s.router_model, api_key=s.groq_api_key, temperature=0).with_structured_output(rag.Route)
        chain = ChatPromptTemplate.from_messages(
            [("system", answer_prompt), MessagesPlaceholder("history"), ("human", "{question}")]
        ) | ChatGroq(model=s.groq_model, api_key=s.groq_api_key, temperature=0) | StrOutputParser()
        return router, chain, answer_prompt

    async def gemini_answer(self, answer_prompt: str, context: str, question: str) -> str:
        T = self.types
        response = await self.client.aio.models.generate_content(
            model=self.args.answer_model,
            contents=question,
            config=T.GenerateContentConfig(
                system_instruction=answer_prompt.replace("{context}", context),
                temperature=0,
                thinking_config=T.ThinkingConfig(thinking_level="MINIMAL"),
            ),
        )
        return (response.text or "").strip()

    async def text(self):
        from backend.rag import format_docs, to_messages

        router, chain, answer_prompt = self.proposed_chains()
        en_pages = [pages(self.rag.retrieve(q)) for q in QUESTIONS["en"]]
        items = []

        async def one(lang, kind, question, history=(), en_ref=None):
            item = {"lang": lang, "kind": kind, "question": question}
            try:
                started = time.monotonic()
                current = await asyncio.to_thread(self.rag.answer, question, list(history))
                item["current_answer"] = current["answer"]
                item["current_answer_in_lang"] = in_script(current["answer"], lang)
                item["current_s"] = round(time.monotonic() - started, 2)

                messages = to_messages(list(history))
                route = await asyncio.to_thread(router.invoke, {"history": messages, "question": question})
                item["intent"] = route.intent
                if route.intent == "small_talk" and route.reply.strip():
                    item["reply"] = route.reply
                    item["reply_in_lang"] = in_script(route.reply, lang)
                    return item
                item["search_query"] = route.search_query
                item["query_is_english"] = in_script(route.search_query, "en", 0.9)
                docs = self.rag.retrieve(route.search_query.strip() or question)
                if en_ref is not None:
                    item["pages_native_query"] = pages(self.rag.retrieve(question))
                    item["pages_translated_query"] = pages(docs)
                    item["pages_english_question"] = en_ref
                    item["overlap_native"] = overlap(item["pages_native_query"], en_ref)
                    item["overlap_translated"] = overlap(item["pages_translated_query"], en_ref)
                context = format_docs(docs)
                started = time.monotonic()
                groq = await asyncio.to_thread(
                    chain.invoke, {"context": context, "history": messages, "question": question}
                )
                item["groq_s"] = round(time.monotonic() - started, 2)
                item["groq_answer"] = groq
                item["groq_answer_in_lang"] = in_script(groq, lang)
                item["groq_script"] = script_mix(groq)
                started = time.monotonic()
                gem = await self.gemini_answer(answer_prompt, context, question)
                item["gemini_s"] = round(time.monotonic() - started, 2)
                item["gemini_answer"] = gem
                item["gemini_answer_in_lang"] = in_script(gem, lang)
            except Exception as e:
                traceback.print_exc()
                item["error"] = f"{type(e).__name__}: {e}"
            return item

        for lang in self.langs:
            for i, q in enumerate(QUESTIONS[lang]):
                items.append(await one(lang, "doc", q, en_ref=en_pages[i]))
                print(f"  {lang} doc {i}: {items[-1].get('search_query')!r}", flush=True)
            items.append(await one(lang, "small_talk", SMALL_TALK[lang]))
            items.append(await one(lang, "not_in_docs", NOT_IN_DOCS[lang]))
            history = [
                {"role": "user", "content": QUESTIONS[lang][0]},
                {"role": "assistant", "content": items[len(items) - 7].get("groq_answer", "")},
            ]
            items.append(await one(lang, "follow_up", FOLLOW_UP[lang], history))

        def rate(key, kinds=("doc", "not_in_docs", "follow_up")):
            vals = [i[key] for i in items if i["kind"] in kinds and key in i]
            return f"{sum(vals)}/{len(vals)}"

        docs = [i for i in items if i["kind"] == "doc" and "overlap_native" in i]
        return {
            "ok": not any("error" in i for i in items),
            "current_prompts_answer_in_lang": rate("current_answer_in_lang", ("doc", "small_talk", "not_in_docs", "follow_up")),
            "query_is_english": rate("query_is_english"),
            "mean_overlap_native_query": round(sum(i["overlap_native"] for i in docs) / len(docs), 2) if docs else None,
            "mean_overlap_translated_query": round(sum(i["overlap_translated"] for i in docs) / len(docs), 2) if docs else None,
            "groq_answer_in_lang": rate("groq_answer_in_lang"),
            "gemini_answer_in_lang": rate("gemini_answer_in_lang"),
            "small_talk_reply_in_lang": rate("reply_in_lang", ("small_talk",)),
            "median_groq_s": median([i.get("groq_s") for i in items]),
            "median_gemini_s": median([i.get("gemini_s") for i in items]),
            "items": items,
        }

    def connect(self, system, tools=None):
        config = live_config(self.types, self.args.live_model, self.args.voice, system, tools)
        return self.client.aio.live.connect(model=self.args.live_model, config=config)

    def spoken(self, lang):
        return [(text, OUT_DIR / f"{lang}_q{i:02d}.wav") for i, text in enumerate([*QUESTIONS[lang], SMALL_TALK[lang]])]

    async def speech(self):
        results = []
        for lang in self.langs:
            for text, path in self.spoken(lang):
                if path.exists():
                    results.append({"lang": lang, "text": text, "cached": True})
                    continue
                best = None
                for attempt in range(1, 6):
                    async with self.connect(TTS_PROMPT) as s:
                        await s.send_realtime_input(text=f'Read aloud: "{text}"')
                        turn = await asyncio.wait_for(collect_turn(s, self.types), 60)
                    match = similarity(text, turn.output_text)
                    if best is None or match > best[0]:
                        best = (match, turn, attempt)
                    if match > 0.8:
                        break
                match, turn, attempt = best
                save_wav(path, resample(bytes(turn.audio), OUT_RATE, IN_RATE), IN_RATE)
                results.append(
                    {"lang": lang, "text": text, "spoken": turn.output_text.strip(), "match": match, "attempts": attempt}
                )
                print(f"  {path.name}: match {match} after {attempt}", flush=True)
        return {"ok": all(r.get("cached") or r["match"] > 0.6 for r in results), "items": results}

    async def transcribe(self):
        from backend.transcribe import PROMPT as current_prompt
        from backend.transcribe import to_wav

        T = self.types
        items = []
        for lang in self.langs:
            for text, path in self.spoken(lang):
                wav = to_wav(load_wav(path))
                hinted = f"The speaker is speaking {LANG_NAME[lang]}. " + TRANSCRIBE_PROMPT_NEW
                prompts = (("current", current_prompt), ("proposed", TRANSCRIBE_PROMPT_NEW), ("hinted", hinted))
                for name, prompt in prompts:
                    await asyncio.sleep(self.args.pause)  # stay under the per-minute quota
                    started = time.monotonic()
                    try:
                        r = await self.client.aio.models.generate_content(
                            model=self.args.transcribe_model,
                            contents=[T.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt],
                            config=T.GenerateContentConfig(
                                temperature=0, thinking_config=T.ThinkingConfig(thinking_level="MINIMAL")
                            ),
                        )
                        out = (r.text or "").strip()
                    except Exception as e:
                        out = f"ERROR {type(e).__name__}: {e}"
                    items.append(
                        {
                            "lang": lang, "prompt": name, "expected": text, "transcript": out,
                            "in_script": in_script(out, lang), "script": script_mix(out),
                            "match": similarity(text, out), "s": round(time.monotonic() - started, 2),
                        }
                    )
        summary = {}
        for name in ("current", "proposed", "hinted"):
            sel = [i for i in items if i["prompt"] == name]
            summary[name] = {
                "in_script": f"{sum(i['in_script'] for i in sel)}/{len(sel)}",
                "median_match": median([i["match"] for i in sel]),
                "median_s": median([i["s"] for i in sel]),
            }
        return {"ok": True, **summary, "items": items}

    async def live(self):
        from backend.live import AGENT_PROMPT
        from backend.rag import format_docs

        tool_queries = []

        async def search(name, args):
            tool_queries.append(args.get("query", ""))
            docs = await asyncio.to_thread(self.rag.retrieve, args.get("query", ""))
            return {"result": format_docs(docs)}

        items = []
        for lang in self.langs:
            spoken = self.spoken(lang)
            order = [spoken[-1], *spoken[:3]]  # small talk first, then three questions
            try:
                async with self.connect(AGENT_PROMPT + AGENT_RULE, tools=[SEARCH_TOOL]) as s:
                    for text, path in order:
                        tool_queries.clear()
                        collector = asyncio.create_task(collect_turn(s, self.types, search))
                        speech_end = await stream_audio(s, self.types, load_wav(path), realtime=True, trailing_silence_s=2)
                        turn = await asyncio.wait_for(collector, 90)
                        save_wav(OUT_DIR / f"live_{lang}_answer{len(items):02d}.wav", bytes(turn.audio), OUT_RATE)
                        items.append(
                            {
                                "lang": lang, "asked": text,
                                "heard": turn.input_text.strip(), "heard_script": script_mix(turn.input_text),
                                "heard_in_script": in_script(turn.input_text, lang),
                                "tool_queries": list(tool_queries),
                                "query_is_english": bool(tool_queries) and all(in_script(q, "en", 0.9) for q in tool_queries),
                                "answer": turn.output_text.strip(), "answer_script": script_mix(turn.output_text),
                                "answer_in_lang": in_script(turn.output_text, lang),
                                "first_audio_s": latency(turn, speech_end),
                            }
                        )
                        print(f"  {lang}: heard {items[-1]['heard'][:60]!r} -> {tool_queries}", flush=True)
            except Exception as e:
                traceback.print_exc()
                items.append({"lang": lang, "error": f"{type(e).__name__}: {e}"})

        ok_items = [i for i in items if "error" not in i]
        questions = [i for i in ok_items if i["asked"] not in SMALL_TALK.values()]
        rate = lambda key, sel: f"{sum(i[key] for i in sel)}/{len(sel)}"
        return {
            "ok": len(ok_items) == len(items),
            "heard_in_script": rate("heard_in_script", ok_items),
            "query_is_english": rate("query_is_english", questions),
            "answer_in_lang": rate("answer_in_lang", ok_items),
            "median_first_audio_s": median([i["first_audio_s"] for i in questions]),
            "items": items,
        }


def use_db_copy():
    """Point the backend settings at a copy of the Milvus Lite database (the backend holds the original)."""
    src = Path(os.environ.get("MILVUS_DB_PATH", "./data/milvus.db"))
    dst = OUT_DIR / "milvus_copy.db"
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
    os.environ["MILVUS_DB_PATH"] = str(dst)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default="text,speech,transcribe,live")
    parser.add_argument("--langs", default="hi,te", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    parser.add_argument("--live-model", default=os.environ.get("LIVE_MODEL", "gemini-3.1-flash-live-preview"))
    parser.add_argument("--voice", default=os.environ.get("LIVE_VOICE", "Kore"))
    parser.add_argument("--transcribe-model", default=os.environ.get("TRANSCRIBE_MODEL", "gemini-3.5-flash-lite"))
    parser.add_argument("--pause", type=float, default=4, help="Seconds between transcription calls")
    parser.add_argument("--answer-model", default="gemini-3.5-flash-lite", help="Gemini model compared with Groq")
    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY in .env first.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    if {"text", "live"} & set(checks):
        use_db_copy()

    precheck = Precheck(args)
    report_path = OUT_DIR / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    for name in checks:
        print(f"\n=== {name}", flush=True)
        started = time.monotonic()
        try:
            result = await getattr(precheck, name)()
        except Exception as e:
            traceback.print_exc()
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["took_s"] = round(time.monotonic() - started, 1)
        report[name] = result
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, indent=2, ensure_ascii=False), flush=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\nFull report: {report_path}")


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
