"""Phase M1 test: multilingual typed chat through the real POST /chat.

Cases:
  langs        per language: 3 document questions, small talk and a not-in-documents question. Checks the
               answer language and how many of the English question's source pages come back
  english      the original English questions: English answers and the same pages as plain retrieval
               (guards the 2026-10-09 router fix)
  conversation short or follow-up messages that depend on the conversation's language
  fallback     a bad ANSWER_MODEL must still produce an answer (Groq fallback); runs RAGChain in-process

Needs lingua on PYTHONPATH (see scripts/multilang_broad.py) and a backend on a copy of the database:

    MILVUS_DB_PATH=./data/m1/milvus_copy.db uvicorn backend.main:app --port 8003
    PYTHONPATH=<lingua dir> python -m scripts.multilang_chat_test --url http://localhost:8003
"""

import argparse
import json
import os
import time
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

from scripts.live_spike import QUESTIONS as EN_QUESTIONS
from scripts.multilang_broad import EN, LANGS, lang_ok
from scripts.multilang_precheck import FOLLOW_UP, NOT_IN_DOCS, QUESTIONS as HI_TE, SMALL_TALK, median, overlap

OUT_DIR = Path("data/m1")

# Hindi and Telugu from M0, in the same shape as the M0b languages (habit loop, habit stacking, environment)
EXTRA = {
    code: (name, lingua, script, [HI_TE[code][0], HI_TE[code][2], HI_TE[code][4], SMALL_TALK[code], NOT_IN_DOCS[code]])
    for code, name, lingua, script in (("hi", "Hindi", "HINDI", "devanagari"), ("te", "Telugu", "TELUGU", "telugu"))
}
LANGS.update(EXTRA)


class Client:
    def __init__(self, url: str, pause: float):
        self.url = url.rstrip("/")
        self.pause = pause

    def chat(self, question, history=(), language=None) -> dict:
        time.sleep(self.pause)
        started = time.monotonic()
        r = requests.post(
            f"{self.url}/chat",
            json={"question": question, "history": list(history), "language": language},
            timeout=180,
        )
        r.raise_for_status()
        return {**r.json(), "s": round(time.monotonic() - started, 2)}


def pages(sources) -> list:
    return [s["page"] for s in sources]


def run_langs(client: Client, langs: list[str], en_pages: list[list]) -> dict:
    items = []
    for code in langs:
        qs = LANGS[code][3]
        for kind, i in (("doc", 0), ("doc", 1), ("doc", 2), ("small_talk", 3), ("not_in_docs", 4)):
            item = {"lang": code, "kind": kind, "question": qs[i]}
            try:
                r = client.chat(qs[i])
                item.update(answer=r["answer"], detected=r["language"], s=r["s"], ok=lang_ok(r["answer"], code))
                if kind == "doc":
                    item["overlap"] = overlap(pages(r["sources"]), en_pages[i])
            except Exception as e:
                traceback.print_exc()
                item["error"] = f"{type(e).__name__}: {e}"
            items.append(item)
        mine = [i for i in items if i["lang"] == code]
        print(f"  {code}: ok {sum(i.get('ok', False) for i in mine)}/5 overlap {[i.get('overlap') for i in mine if i['kind'] == 'doc']} detected {mine[0].get('detected')}", flush=True)

    done = [i for i in items if "error" not in i]
    docs = [i for i in done if i["kind"] == "doc"]
    rate = lambda sel: f"{sum(i['ok'] for i in sel)}/{len(sel)}"
    per_lang = {
        code: {
            "ok": rate([i for i in done if i["lang"] == code]),
            "overlap": round(sum(i["overlap"] for i in docs if i["lang"] == code) / max(1, sum(i["lang"] == code for i in docs)), 2),
            "detected": sorted({i["detected"] for i in done if i["lang"] == code}),
        }
        for code in langs
    }
    return {
        "answer_in_lang": rate(done),
        "doc_answers_in_lang": rate(docs),
        "small_talk_in_lang": rate([i for i in done if i["kind"] == "small_talk"]),
        "not_found_in_lang": rate([i for i in done if i["kind"] == "not_in_docs"]),
        "mean_overlap": round(sum(i["overlap"] for i in docs) / max(1, len(docs)), 2),
        "errors": len(items) - len(done),
        "median_s": median([i["s"] for i in done]),
        "per_lang": per_lang,
        "items": items,
    }


def run_english(client: Client, baseline: dict) -> dict:
    items = []
    for q in EN_QUESTIONS:
        r = client.chat(q)
        items.append(
            {"question": q, "answer": r["answer"], "detected": r["language"], "ok": lang_ok(r["answer"], "en"),
             "overlap": overlap(pages(r["sources"]), baseline[q]), "s": r["s"]}
        )
        print(f"  en: overlap {items[-1]['overlap']} {q}", flush=True)
    return {
        "answer_in_english": f"{sum(i['ok'] for i in items)}/{len(items)}",
        "mean_overlap_vs_plain_retrieval": round(sum(i["overlap"] for i in items) / len(items), 2),
        "median_s": median([i["s"] for i in items]),
        "items": items,
    }


def run_conversation(client: Client) -> dict:
    items = []
    for code in ("hi", "te"):
        first = client.chat(LANGS[code][3][0])
        history = [{"role": "user", "content": LANGS[code][3][0]}, {"role": "assistant", "content": first["answer"]}]
        for name, question, language in (
            ("follow_up", FOLLOW_UP[code], None),  # "tell me more about it", in the language
            ("short_english", "ok, thanks", code),  # language unclear: should follow the conversation
            ("short_question_english", "and the second one?", code),
        ):
            r = client.chat(question, history, language)
            items.append(
                {"lang": code, "case": name, "question": question, "answer": r["answer"], "detected": r["language"],
                 "ok": lang_ok(r["answer"], code), "pages": pages(r["sources"])}
            )
            print(f"  {code} {name}: ok {items[-1]['ok']} detected {r['language']}", flush=True)
    return {"ok": f"{sum(i['ok'] for i in items)}/{len(items)}", "items": items}


def run_fallback() -> dict:
    os.environ["ANSWER_MODEL"] = "gemini-no-such-model"
    from backend.config import get_settings
    from backend.rag import RAGChain

    get_settings.cache_clear()
    result = RAGChain().answer(LANGS["te"][3][0], [])
    return {"ok": bool(result["answer"]) and lang_ok(result["answer"], "te"), "answer": result["answer"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8003")
    parser.add_argument("--cases", default="english,langs,conversation,fallback")
    parser.add_argument("--langs", default=",".join(LANGS), type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    parser.add_argument("--pause", type=float, default=3, help="Seconds between requests (Groq and Gemini quotas)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = Client(args.url, args.pause)
    report_path = OUT_DIR / "chat_report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}

    # Reference pages: plain retrieval of the English question, as the backend did before M1
    from backend.vectorstore import get_vectorstore

    if os.environ.get("MILVUS_DB_PATH") is None:
        raise SystemExit("Set MILVUS_DB_PATH to a database copy (the running backends hold theirs).")
    vs = get_vectorstore()
    plain = lambda q: [d.metadata.get("page") for d in vs.similarity_search(q, k=4)]
    en_pages = [plain(q) for q in EN]
    baseline = {q: plain(q) for q in EN_QUESTIONS}

    for case in [c.strip() for c in args.cases.split(",") if c.strip()]:
        print(f"\n=== {case}", flush=True)
        started = time.monotonic()
        try:
            result = {
                "langs": lambda: run_langs(client, args.langs, en_pages),
                "english": lambda: run_english(client, baseline),
                "conversation": lambda: run_conversation(client),
                "fallback": run_fallback,
            }[case]()
        except Exception as e:
            traceback.print_exc()
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["took_s"] = round(time.monotonic() - started, 1)
        report[case] = result
        print(json.dumps({k: v for k, v in result.items() if k not in ("items", "per_lang")}, indent=2, ensure_ascii=False), flush=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nFull report: {report_path}")


if __name__ == "__main__":
    load_dotenv()
    main()
