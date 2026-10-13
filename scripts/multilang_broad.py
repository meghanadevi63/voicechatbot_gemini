"""Phase M0b: broad multilingual precheck across the Tier 2 languages (see MULTILANGUAGE_PLAN.md).

Checks (pick with --checks):
  text        per language: proposed router (English query?), retrieval overlap with the English question,
              answer language and tokens for Groq gpt-oss-20b and Gemini Flash Lite
  speech      spoken questions for the speech subset (Gemini Live reads each aloud), cached as 16 kHz WAV
  transcribe  dictation without and with a language hint (speech subset)
  live        hands-free agent mode with the proposed prompt (speech subset)

Language identity is checked with lingua (an offline language detector) plus the expected script; Kannada,
which lingua lacks, is checked by script only. Install lingua outside the project venv and put it on
PYTHONPATH, e.g.  uv pip install --target /some/dir lingua-language-detector

    PYTHONPATH=/some/dir python -m scripts.multilang_broad --checks text
    PYTHONPATH=/some/dir python -m scripts.multilang_broad --checks speech,transcribe,live --langs ar,ja
"""

import argparse
import asyncio
import json
import os
import shutil
import time
import traceback
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
from scripts.multilang_precheck import AGENT_RULE, ROUTER_RULE, TRANSCRIBE_PROMPT_NEW, median, overlap, pages, similarity

OUT_DIR = Path("data/multilang_precheck")

# Proposed answer rule, tightened after the first precheck (no English glosses in parentheses)
ANSWER_RULE = """
- The excerpts are in English, but reply in the language of the user's latest message, in that language's
  usual script. If the message is too short to tell its language, use the language of the conversation so
  far. This includes saying you couldn't find something.
- Don't add English translations of terms in parentheses."""

# Index into the English questions: habit loop, habit stacking (jargon), environment (hardest in M0)
EN = ["What is the habit loop?", "What is habit stacking?", "How does our environment affect our habits?"]
SMALL_TALK_EN = "Hi, how are you?"
NOT_IN_DOCS_EN = "What is the capital of France?"

# lang: (name, lingua name or None, expected script, [3 doc questions, small talk, not in docs])
LANGS = {
    "es": ("Spanish", "SPANISH", "latin", ["¿Qué es el ciclo del hábito?", "¿Qué es el apilamiento de hábitos?", "¿Cómo afecta el entorno a nuestros hábitos?", "Hola, ¿cómo estás?", "¿Cuál es la capital de Francia?"]),
    "fr": ("French", "FRENCH", "latin", ["Qu'est-ce que la boucle de l'habitude ?", "Qu'est-ce que l'empilement d'habitudes ?", "Comment notre environnement influence-t-il nos habitudes ?", "Bonjour, comment ça va ?", "Quelle est la capitale de la France ?"]),
    "de": ("German", "GERMAN", "latin", ["Was ist die Gewohnheitsschleife?", "Was bedeutet das Stapeln von Gewohnheiten?", "Wie beeinflusst unsere Umgebung unsere Gewohnheiten?", "Hallo, wie geht es dir?", "Was ist die Hauptstadt von Frankreich?"]),
    "pt": ("Portuguese", "PORTUGUESE", "latin", ["O que é o ciclo do hábito?", "O que é o empilhamento de hábitos?", "Como o ambiente afeta nossos hábitos?", "Oi, tudo bem com você?", "Qual é a capital da França?"]),
    "ru": ("Russian", "RUSSIAN", "cyrillic", ["Что такое петля привычки?", "Что такое объединение привычек в цепочку?", "Как окружающая среда влияет на наши привычки?", "Привет, как дела?", "Какая столица Франции?"]),
    "ar": ("Arabic", "ARABIC", "arabic", ["ما هي حلقة العادة؟", "ما هو تكديس العادات؟", "كيف تؤثر البيئة المحيطة على عاداتنا؟", "مرحبًا، كيف حالك؟", "ما هي عاصمة فرنسا؟"]),
    "ur": ("Urdu", "URDU", "arabic", ["عادت کا چکر کیا ہے؟", "ہیبٹ اسٹیکنگ کیا ہے؟", "ہمارا ماحول ہماری عادتوں پر کیسے اثر ڈالتا ہے؟", "السلام علیکم، آپ کیسے ہیں؟", "فرانس کا دارالحکومت کیا ہے؟"]),
    "bn": ("Bengali", "BENGALI", "bengali", ["অভ্যাসের চক্র কী?", "হ্যাবিট স্ট্যাকিং কী?", "আমাদের পরিবেশ কীভাবে আমাদের অভ্যাসকে প্রভাবিত করে?", "নমস্কার, আপনি কেমন আছেন?", "ফ্রান্সের রাজধানী কী?"]),
    "ta": ("Tamil", "TAMIL", "tamil", ["பழக்க வளையம் என்றால் என்ன?", "ஹேபிட் ஸ்டாக்கிங் என்றால் என்ன?", "நம் சுற்றுச்சூழல் நம் பழக்கங்களை எப்படி பாதிக்கிறது?", "வணக்கம், எப்படி இருக்கிறீர்கள்?", "பிரான்சின் தலைநகரம் எது?"]),
    "kn": ("Kannada", None, "kannada", ["ಅಭ್ಯಾಸದ ಚಕ್ರ ಎಂದರೇನು?", "ಹ್ಯಾಬಿಟ್ ಸ್ಟ್ಯಾಕಿಂಗ್ ಎಂದರೇನು?", "ನಮ್ಮ ಪರಿಸರ ನಮ್ಮ ಅಭ್ಯಾಸಗಳ ಮೇಲೆ ಹೇಗೆ ಪರಿಣಾಮ ಬೀರುತ್ತದೆ?", "ನಮಸ್ಕಾರ, ಹೇಗಿದ್ದೀರಾ?", "ಫ್ರಾನ್ಸ್‌ನ ರಾಜಧಾನಿ ಯಾವುದು?"]),
    "mr": ("Marathi", "MARATHI", "devanagari", ["सवयींचे चक्र म्हणजे काय?", "हॅबिट स्टॅकिंग म्हणजे काय?", "आपले वातावरण आपल्या सवयींवर कसा परिणाम करते?", "नमस्कार, तुम्ही कसे आहात?", "फ्रान्सची राजधानी कोणती आहे?"]),
    "gu": ("Gujarati", "GUJARATI", "gujarati", ["આદતનું ચક્ર શું છે?", "હેબિટ સ્ટેકિંગ શું છે?", "આપણું વાતાવરણ આપણી આદતો પર કેવી અસર કરે છે?", "નમસ્તે, તમે કેમ છો?", "ફ્રાન્સની રાજધાની કઈ છે?"]),
    "zh": ("Chinese", "CHINESE", "han", ["什么是习惯循环？", "什么是习惯叠加？", "环境如何影响我们的习惯？", "你好，你最近怎么样？", "法国的首都是哪里？"]),
    "ja": ("Japanese", "JAPANESE", "japanese", ["習慣のループとは何ですか？", "習慣の積み重ねとは何ですか？", "環境は私たちの習慣にどのように影響しますか？", "こんにちは、お元気ですか？", "フランスの首都はどこですか？"]),
    "ko": ("Korean", "KOREAN", "hangul", ["습관 고리란 무엇인가요?", "습관 쌓기란 무엇인가요?", "환경은 우리의 습관에 어떤 영향을 미치나요?", "안녕하세요, 잘 지내세요?", "프랑스의 수도는 어디인가요?"]),
    "th": ("Thai", "THAI", "thai", ["วงจรนิสัยคืออะไร", "การต่อยอดนิสัยคืออะไร", "สภาพแวดล้อมส่งผลต่อนิสัยของเราอย่างไร", "สวัสดี สบายดีไหม", "เมืองหลวงของฝรั่งเศสคืออะไร"]),
    "id": ("Indonesian", "INDONESIAN", "latin", ["Apa itu lingkaran kebiasaan?", "Apa itu penumpukan kebiasaan?", "Bagaimana lingkungan memengaruhi kebiasaan kita?", "Halo, apa kabar?", "Apa ibu kota Prancis?"]),
    "vi": ("Vietnamese", "VIETNAMESE", "latin", ["Vòng lặp thói quen là gì?", "Xếp chồng thói quen là gì?", "Môi trường ảnh hưởng đến thói quen của chúng ta như thế nào?", "Xin chào, bạn khỏe không?", "Thủ đô của Pháp là gì?"]),
    "tr": ("Turkish", "TURKISH", "latin", ["Alışkanlık döngüsü nedir?", "Alışkanlık istifleme nedir?", "Çevremiz alışkanlıklarımızı nasıl etkiler?", "Merhaba, nasılsın?", "Fransa'nın başkenti neresidir?"]),
    "sw": ("Swahili", "SWAHILI", "latin", ["Mzunguko wa tabia ni nini?", "Kuunganisha tabia ni nini?", "Mazingira yanaathiri vipi tabia zetu?", "Habari, hujambo?", "Mji mkuu wa Ufaransa ni upi?"]),
}
SPEECH_SUBSET = ["ar", "zh", "ja", "ta", "es", "ru"]

SCRIPTS = {
    "devanagari": [(0x0900, 0x097F)], "bengali": [(0x0980, 0x09FF)], "gujarati": [(0x0A80, 0x0AFF)],
    "tamil": [(0x0B80, 0x0BFF)], "telugu": [(0x0C00, 0x0C7F)], "kannada": [(0x0C80, 0x0CFF)],
    "thai": [(0x0E00, 0x0E7F)], "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFEFF)],
    "cyrillic": [(0x0400, 0x04FF)], "hangul": [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "kana": [(0x3040, 0x30FF)], "han": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "latin": [(0x0041, 0x024F), (0x1E00, 0x1EFF)],
}


def script_mix(text: str) -> dict:
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        name = next((n for n, ranges in SCRIPTS.items() if any(lo <= cp <= hi for lo, hi in ranges)), "other")
        counts[name] = counts.get(name, 0) + 1
    total = sum(counts.values()) or 1
    return {k: round(v / total, 2) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])}


def script_share(text: str, script: str) -> float:
    mix = script_mix(text)
    if script == "japanese":  # kana plus kanji
        return mix.get("kana", 0) + mix.get("han", 0)
    return mix.get(script, 0)


_detector = None


def detect(text: str) -> str | None:
    global _detector
    from lingua import LanguageDetectorBuilder

    if _detector is None:
        _detector = LanguageDetectorBuilder.from_all_languages().build()
    lang = _detector.detect_language_of(text)
    return lang.name if lang else None


def lang_ok(text: str, code: str) -> bool:
    """The text is in the expected language: the expected script dominates and, where lingua knows the language,
    lingua agrees."""
    if not text.strip():
        return False
    _, lingua_name, script, _ = LANGS[code] if code in LANGS else (None, "ENGLISH", "latin", None)
    if script_share(text, script) < 0.6:
        return False
    # Indonesian and Malay are close enough that lingua confuses short texts
    accepted = {"INDONESIAN": {"INDONESIAN", "MALAY"}}.get(lingua_name, {lingua_name})
    return lingua_name is None or detect(text) in accepted


def is_english(text: str) -> bool:
    return bool(text.strip()) and script_share(text, "latin") >= 0.9 and detect(text) == "ENGLISH"


async def gemini_call(fn, *args, tries=5, **kwargs):
    """Call Gemini, waiting and retrying on 429 (quota)."""
    for attempt in range(tries):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            if "429" not in str(e) or attempt == tries - 1:
                raise
            await asyncio.sleep(15 * (attempt + 1))


class Broad:
    def __init__(self, args):
        from google import genai
        from google.genai import types

        self.args = args
        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._rag = None

    @property
    def rag(self):
        if self._rag is None:
            from backend.rag import RAGChain

            self._rag = RAGChain()
        return self._rag

    # --- text ---

    def chains(self):
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_groq import ChatGroq

        from backend import rag
        from backend.config import get_settings

        s = get_settings()
        # M1 moved a language rule into SYSTEM_PROMPT; fill its slot generically so this precheck still runs
        base = rag.SYSTEM_PROMPT.replace("{reply_language}", "reply in the language of the user's latest message")
        answer_prompt = base.replace("\n\nDocument excerpts:", ANSWER_RULE + "\n\nDocument excerpts:")
        groq = lambda model: ChatGroq(model=model, api_key=s.groq_api_key, temperature=0, max_retries=8)
        router = ChatPromptTemplate.from_messages(
            [("system", rag.ROUTER_PROMPT + ROUTER_RULE), MessagesPlaceholder("history"), ("human", "{question}")]
        ) | groq(s.router_model).with_structured_output(rag.Route)
        # No output parser: keep the message so token usage can be read
        chain = ChatPromptTemplate.from_messages(
            [("system", answer_prompt), MessagesPlaceholder("history"), ("human", "{question}")]
        ) | groq(s.groq_model)
        return router, chain, answer_prompt

    async def gemini_answer(self, answer_prompt, context, question):
        T = self.types
        r = await gemini_call(
            self.client.aio.models.generate_content,
            model=self.args.answer_model,
            contents=question,
            config=T.GenerateContentConfig(
                system_instruction=answer_prompt.replace("{context}", context),
                temperature=0,
                thinking_config=T.ThinkingConfig(thinking_level="MINIMAL"),
            ),
        )
        tokens = r.usage_metadata.candidates_token_count if r.usage_metadata else None
        return (r.text or "").strip(), tokens

    async def text(self):
        from backend.rag import format_docs

        router, chain, answer_prompt = self.chains()
        en_pages = [pages(self.rag.retrieve(q)) for q in EN]
        en_tokens = []
        items = []

        async def one(code, kind, question, en_ref=None):
            item = {"lang": code, "kind": kind, "question": question}
            try:
                route = await asyncio.to_thread(router.invoke, {"history": [], "question": question})
                item["intent"] = route.intent
                if route.intent == "small_talk" and route.reply.strip():
                    item["reply"] = route.reply
                    item["reply_ok"] = lang_ok(route.reply, code)
                    return item
                item["search_query"] = route.search_query
                item["query_english"] = is_english(route.search_query)
                docs = self.rag.retrieve(route.search_query.strip() or question)
                if en_ref is not None:
                    item["overlap"] = overlap(pages(docs), en_ref)
                context = format_docs(docs)
                started = time.monotonic()
                msg = await asyncio.to_thread(chain.invoke, {"context": context, "history": [], "question": question})
                item["groq_s"] = round(time.monotonic() - started, 2)
                item["groq_answer"] = msg.content
                item["groq_tokens"] = (msg.usage_metadata or {}).get("output_tokens")
                item["groq_ok"] = lang_ok(msg.content, code)
                item["groq_markdown"] = any(m in msg.content for m in ("**", "\n- ", "\n1.", "#"))
                await asyncio.sleep(self.args.pause)
                started = time.monotonic()
                item["gemini_answer"], item["gemini_tokens"] = await self.gemini_answer(answer_prompt, context, question)
                item["gemini_s"] = round(time.monotonic() - started, 2)
                item["gemini_ok"] = lang_ok(item["gemini_answer"], code)
            except Exception as e:
                traceback.print_exc()
                item["error"] = f"{type(e).__name__}: {e}"
            return item

        # English reference for token counts
        for q in EN:
            msg = await asyncio.to_thread(
                chain.invoke, {"context": format_docs(self.rag.retrieve(q)), "history": [], "question": q}
            )
            en_tokens.append((msg.usage_metadata or {}).get("output_tokens"))

        for code in self.args.langs:
            qs = LANGS[code][3]
            for i in range(3):
                items.append(await one(code, "doc", qs[i], en_pages[i]))
            items.append(await one(code, "small_talk", qs[3]))
            items.append(await one(code, "not_in_docs", qs[4]))
            mine = [i for i in items if i["lang"] == code]
            print(
                f"  {code}: queries {[i.get('search_query') for i in mine if i['kind'] == 'doc']} "
                f"groq_ok {sum(i.get('groq_ok', False) for i in mine)} gemini_ok {sum(i.get('gemini_ok', False) for i in mine)}",
                flush=True,
            )

        per_lang = {}
        for code in self.args.langs:
            mine = [i for i in items if i["lang"] == code]
            answered = [i for i in mine if "groq_answer" in i]
            docs = [i for i in mine if "overlap" in i]
            per_lang[code] = {
                "query_english": f"{sum(i['query_english'] for i in answered)}/{len(answered)}",
                "overlap": round(sum(i["overlap"] for i in docs) / len(docs), 2) if docs else None,
                "groq_ok": f"{sum(i['groq_ok'] for i in answered)}/{len(answered)}",
                "gemini_ok": f"{sum(i['gemini_ok'] for i in answered)}/{len(answered)}",
                "small_talk_ok": next((i.get("reply_ok") for i in mine if i["kind"] == "small_talk"), None),
                "groq_tokens": median([i.get("groq_tokens") for i in answered]),
                "gemini_tokens": median([i.get("gemini_tokens") for i in answered]),
                "errors": sum("error" in i for i in mine),
            }
        answered = [i for i in items if "groq_answer" in i]
        total = lambda key: f"{sum(i[key] for i in answered)}/{len(answered)}"
        return {
            "ok": not any("error" in i for i in items),
            "query_english": total("query_english"),
            "mean_overlap": round(sum(i["overlap"] for i in items if "overlap" in i) / max(1, sum("overlap" in i for i in items)), 2),
            "groq_ok": total("groq_ok"),
            "gemini_ok": total("gemini_ok"),
            "groq_markdown": total("groq_markdown"),
            "small_talk_ok": f"{sum(i.get('reply_ok', False) for i in items if i['kind'] == 'small_talk')}/{sum(i['kind'] == 'small_talk' for i in items)}",
            "median_groq_s": median([i.get("groq_s") for i in items]),
            "median_gemini_s": median([i.get("gemini_s") for i in items]),
            "english_answer_tokens": median(en_tokens),
            "per_lang": per_lang,
            "items": items,
        }

    # --- speech, transcribe, live (speech subset) ---

    def connect(self, system, tools=None):
        config = live_config(self.types, self.args.live_model, self.args.voice, system, tools)
        return self.client.aio.live.connect(model=self.args.live_model, config=config)

    def spoken(self, code):
        qs = LANGS[code][3]
        return [(qs[i], OUT_DIR / f"{code}_q{i:02d}.wav") for i in (0, 1, 2, 3)]

    def subset(self):
        return [c for c in self.args.langs if c in SPEECH_SUBSET] or SPEECH_SUBSET

    async def speech(self):
        results = []
        for code in self.subset():
            for text, path in self.spoken(code):
                if path.exists():
                    results.append({"lang": code, "text": text, "cached": True})
                    continue
                best = None
                for attempt in range(1, 6):
                    async with self.connect(TTS_PROMPT) as s:
                        await s.send_realtime_input(text=f'Read aloud: "{text}"')
                        turn = await asyncio.wait_for(collect_turn(s, self.types), 60)
                    # CJK and Thai have no spaces, so compare characters there
                    match = similarity(" ".join(text), " ".join(turn.output_text)) if code in ("zh", "ja", "th") else similarity(text, turn.output_text)
                    if best is None or match > best[0]:
                        best = (match, turn, attempt)
                    if match > 0.8:
                        break
                match, turn, attempt = best
                save_wav(path, resample(bytes(turn.audio), OUT_RATE, IN_RATE), IN_RATE)
                results.append({"lang": code, "text": text, "spoken": turn.output_text.strip(), "match": match, "attempts": attempt})
                print(f"  {path.name}: match {match} after {attempt}", flush=True)
        return {"ok": all(r.get("cached") or r["match"] > 0.6 for r in results), "items": results}

    async def transcribe(self):
        from backend.transcribe import to_wav

        T = self.types
        items = []
        for code in self.subset():
            for text, path in self.spoken(code):
                wav = to_wav(load_wav(path))
                hinted = f"The speaker is speaking {LANGS[code][0]}. " + TRANSCRIBE_PROMPT_NEW
                for name, prompt in (("no_hint", TRANSCRIBE_PROMPT_NEW), ("hinted", hinted)):
                    await asyncio.sleep(self.args.pause)
                    started = time.monotonic()
                    try:
                        r = await gemini_call(
                            self.client.aio.models.generate_content,
                            model=self.args.transcribe_model,
                            contents=[T.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt],
                            config=T.GenerateContentConfig(temperature=0, thinking_config=T.ThinkingConfig(thinking_level="MINIMAL")),
                        )
                        out = (r.text or "").strip()
                    except Exception as e:
                        out = f"ERROR {type(e).__name__}: {e}"
                    match = similarity(" ".join(text), " ".join(out)) if code in ("zh", "ja", "th") else similarity(text, out)
                    items.append(
                        {"lang": code, "prompt": name, "expected": text, "transcript": out, "ok": lang_ok(out, code),
                         "match": match, "s": round(time.monotonic() - started, 2)}
                    )
        summary = {}
        for name in ("no_hint", "hinted"):
            sel = [i for i in items if i["prompt"] == name]
            summary[name] = {"ok": f"{sum(i['ok'] for i in sel)}/{len(sel)}", "median_match": median([i["match"] for i in sel]), "median_s": median([i["s"] for i in sel])}
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
        for code in self.subset():
            spoken = self.spoken(code)
            try:
                async with self.connect(AGENT_PROMPT + AGENT_RULE, tools=[SEARCH_TOOL]) as s:
                    for text, path in [spoken[3], *spoken[:3]]:  # small talk first
                        tool_queries.clear()
                        collector = asyncio.create_task(collect_turn(s, self.types, search))
                        speech_end = await stream_audio(s, self.types, load_wav(path), realtime=True, trailing_silence_s=2)
                        turn = await asyncio.wait_for(collector, 90)
                        items.append(
                            {"lang": code, "asked": text, "small_talk": path.name.endswith("q03.wav"),
                             "heard": turn.input_text.strip(), "heard_ok": lang_ok(turn.input_text, code),
                             "tool_queries": list(tool_queries),
                             "query_english": bool(tool_queries) and all(is_english(q) for q in tool_queries),
                             "answer": turn.output_text.strip(), "answer_ok": lang_ok(turn.output_text, code),
                             "first_audio_s": latency(turn, speech_end)}
                        )
                        print(f"  {code}: heard {items[-1]['heard'][:50]!r} -> {tool_queries} answer_ok {items[-1]['answer_ok']}", flush=True)
            except Exception as e:
                traceback.print_exc()
                items.append({"lang": code, "error": f"{type(e).__name__}: {e}"})
        ok = [i for i in items if "error" not in i]
        qs = [i for i in ok if not i["small_talk"]]
        rate = lambda key, sel: f"{sum(i[key] for i in sel)}/{len(sel)}"
        return {
            "ok": len(ok) == len(items),
            "heard_ok": rate("heard_ok", ok), "query_english": rate("query_english", qs),
            "answer_ok": rate("answer_ok", ok), "median_first_audio_s": median([i["first_audio_s"] for i in qs]),
            "items": items,
        }


def use_db_copy(tag: str):
    """Point the backend settings at a private copy of the Milvus Lite database (one per process)."""
    src = Path(os.environ.get("MILVUS_DB_PATH", "./data/milvus.db"))
    dst = OUT_DIR / f"milvus_copy_{tag}.db"
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
    os.environ["MILVUS_DB_PATH"] = str(dst)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default="text")
    parser.add_argument("--langs", default=",".join(LANGS), type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    parser.add_argument("--live-model", default=os.environ.get("LIVE_MODEL", "gemini-3.1-flash-live-preview"))
    parser.add_argument("--voice", default=os.environ.get("LIVE_VOICE", "Kore"))
    parser.add_argument("--transcribe-model", default=os.environ.get("TRANSCRIBE_MODEL", "gemini-3.5-flash-lite"))
    parser.add_argument("--answer-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--pause", type=float, default=4, help="Seconds between Gemini Flash Lite calls")
    parser.add_argument("--report", default="broad_report.json")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    if {"text", "live"} & set(checks):
        use_db_copy("_".join(checks))

    broad = Broad(args)
    report_path = OUT_DIR / args.report
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    for name in checks:
        print(f"\n=== {name}", flush=True)
        started = time.monotonic()
        try:
            result = await getattr(broad, name)()
        except Exception as e:
            traceback.print_exc()
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["took_s"] = round(time.monotonic() - started, 1)
        report[name] = result
        print(json.dumps({k: v for k, v in result.items() if k not in ("items", "per_lang")}, indent=2, ensure_ascii=False), flush=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nFull report: {report_path}")


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
