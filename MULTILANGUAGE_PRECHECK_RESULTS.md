# Multilingual prechecks: results

Results of the prechecks for [MULTILANGUAGE_PLAN.md](MULTILANGUAGE_PLAN.md). Each precheck ran the proposed
prompt changes directly against the models. The backend code wasn't changed, and every run used a copy of the
Milvus database.

| Precheck | Date | Languages | Script | Raw results (in `data/multilang_precheck/`) |
|---|---|---|---|---|
| M0 | 2026-10-13 | Hindi, Telugu | [scripts/multilang_precheck.py](scripts/multilang_precheck.py) | `report.json` |
| M0b | 2026-10-13 | 20 Tier 2 languages for typed chat; Arabic, Chinese, Japanese, Tamil, Spanish and Russian for voice | [scripts/multilang_broad.py](scripts/multilang_broad.py) | `broad_text.json`, `broad_speech.json`, `broad_speech_ar.json` |

All spoken questions were synthetic: Gemini Live read them aloud. Real voices will be harder (see M5 in the
plan).

---

## Summary

1. **The approach works across languages.**
   - In all 20 languages the router turned the question into an English search query (80/80), and the answer
     came back in the user's language.
   - Retrieval found on average 80% of the same pages as the English question, without re-indexing.
   - Today's prompts fail: non-English questions get the English "I couldn't find that in the documents."
2. **Answer writer: Gemini Flash Lite is recommended over Groq `gpt-oss-20b`.**
   - Language accuracy is the same.
   - Gemini added English glosses in parentheses in 1 of 48 non-Latin answers. Groq did it in 12 of 48, even
     when the prompt forbade it.
   - Gemini uses 2 to 4 times fewer output tokens.
   - Gemini isn't affected by Groq's limit of 8,000 tokens per minute.
3. **One prompt bug is common to both models:** the not-found reply is copied word for word from the English
   example in the prompt (7 of 80 answers). The fix is to describe the reply instead of quoting an English
   sentence.
4. **Voice answers are reliable:** 24/24 in the right language, and 17/18 searches used an English query.
5. **Dictation is the weakest part.**
   - With a hint, Hindi and Telugu are reliable.
   - Japanese small talk stayed romanized even with a hint.
   - Tamil and Chinese clips had misheard words with or without a hint.
   - Gemini Live's own transcription heard the same Tamil audio correctly.
6. **Specific terms translate unevenly.** The search translation is the weak spot for book-specific terms:
   - "Habit stacking" became "habit accumulation" (Japanese), "habit building" (Korean) and "habit extension"
     (Thai).
   - Swahili "tabia" (which means habit or behaviour) became "behavior", so retrieval missed (overlap 0.08).

---

## M0: Hindi and Telugu

### Typed chat
5 document questions, small talk, a not-in-documents question and a follow-up, per language.

| Check | Current prompts | Proposed prompts |
|---|---|---|
| Answer in the user's language | **3/16**. 13 of 14 document questions got the English "I couldn't find that in the documents." | Groq **13/14**, Gemini Flash Lite **14/14** |
| Search query in English | n/a: the question is searched as written | **14/14** |
| Retrieved pages shared with the English question's top 4 | **0.00** | **0.82** on average. Four questions 1.0, two 0.75, the environment question 0.5 |
| Small talk reply in the user's language | 2/2 | 2/2 |
| Follow-up ("tell me more about it") | – | Resolved to an English query about the habit loop, in both languages |

| | Groq `gpt-oss-20b` | Gemini `3.5-flash-lite` |
|---|---|---|
| In the right language and script | 13/14. One Hindi answer had 34% English words in parentheses. | 14/14 |
| Quality problems seen | One Telugu answer used a markdown list and wrongly said one of the four laws wasn't in the documents | A few garbled characters in one transliterated book title. Names the book in most answers. |
| Speed | 0.6 to 0.8 s when not throttled | 1.1 to 2.3 s |

### Dictation
6 clips per language.

| Prompt | Right script | Median word match |
|---|---|---|
| Current ("exactly as spoken") | 7/12 | 0.89 |
| Proposed (keep the language and script, don't translate) | 7/12 | 0.82 |
| **Proposed plus a language hint** | **11/12** | **1.00** |

Without a hint, the model:
- romanized words ("Habit stacking kya hai?", "Namaste, meeru ela unnaru?")
- once wrote a Telugu question in Devanagari, as Marathi-like text

### Hands-free
Agent mode, small talk plus 3 questions per language.

| Check | Result |
|---|---|
| Search query in English | 6/6 |
| Answer in the user's language | 8/8 |
| The user's question shown in the right script | 5/8. Telugu small talk came back romanized. |
| End of speech to first audio | median 2.1 s (one 14.4 s outlier) |

### Other findings
- **Groq rate limit:** the key allows **8,000 tokens per minute**. During bursts, calls slow from 0.7 s to 8 to
  14 s, even for English. That's throttling, not language.
- **Gemini quota:** Flash Lite returned `429` after about 20 calls in quick succession.
- **Bug:** `similarity()` in [backend/live.py](backend/live.py) only matches a–z and digits, so the relay log
  would always show "fidelity 1.000" for non-Latin languages.

---

## M0b: 20 languages

Languages: Spanish, French, German, Portuguese, Russian, Arabic, Urdu, Bengali, Tamil, Kannada, Marathi,
Gujarati, Chinese, Japanese, Korean, Thai, Indonesian, Vietnamese, Turkish and Swahili.

**How it was checked:**
- The language of every output was identified with the offline `lingua` detector together with a script check.
  Kannada, which lingua doesn't support, was checked by script only.
- Every failure was then reviewed by hand. The 12 "non-English" search queries the detector flagged were all
  short English phrases like "habit loop" or "capital of France", so they're counted as English below.

**Question set, per language:** 3 document questions (habit loop, habit stacking, environment), small talk, and
a not-in-documents question. The translations were written for this test and haven't been reviewed by native
speakers.

### Typed chat

| Check | Result |
|---|---|
| Search query in English | **80/80** |
| Retrieved pages shared with the English question's top 4 | **0.80** on average |
| Small talk reply in the user's language | **20/20** |
| Document answers in the user's language | Groq **60/60**, Gemini **60/60** (Swahili "habit loop" got a not-found reply from Gemini, in Swahili) |
| Not-found reply in the user's language | Groq 16/20, Gemini 17/20. The failures were the English "I couldn't find that in the documents." in Russian, Korean, Thai, Vietnamese and Turkish. |
| English glosses in parentheses, non-Latin languages | Groq **12/48**, e.g. "(cue)" and "(craving)". Gemini **1/48**. |
| Markdown in answers | Groq 1/80 (bold text in Urdu), Gemini 0/80 |
| Median answer time | Groq 1.8 s (throttled at times), Gemini 2.1 s |
| Median output tokens per answer (English: 123 for Groq) | Groq 140 to 301, Gemini 56 to 96 |

**Retrieval overlap by language**

| Overlap | Languages |
|---|---|
| 0.9 to 1.0 | French, Bengali, Tamil, Gujarati, Spanish, Portuguese, Marathi, Chinese, Indonesian, Vietnamese, Turkish |
| 0.75 to 0.83 | German, Arabic, Kannada |
| 0.58 to 0.67 | Russian, Japanese, Urdu, Korean, Thai: "habit stacking" or "habit loop" was translated as a different term |
| **0.08** | **Swahili:** "tabia" (habit or behaviour) was translated as "behavior" |

### Hands-free
Gemini Live agent mode with the proposed prompt, in Arabic, Chinese, Japanese, Tamil, Spanish and Russian.
Arabic timed out on the first run and passed on a rerun.

| Check | Result |
|---|---|
| Answer in the user's language | **24/24** |
| Search query in English | **17/18**. One Tamil query was sent in Tamil, and retrieval failed. |
| The user's question shown correctly | 23/24. One Tamil transcript contained characters from another script (Amharic). |
| End of speech to first audio | median 2.0 s (2.4 s in the Arabic rerun) |

- **Chinese transcripts have a space between every character** ("你 好 , 你 最 近 怎 么 样 ?"). The chat must remove
  these before showing or saving the text.
- Russian, Spanish, Japanese and Arabic transcripts were shown correctly. Small talk was answered without
  searching in every language.

### Dictation
Gemini Flash Lite, 4 clips per language.

| Prompt | Right language and script | Median match |
|---|---|---|
| No hint | 21/24 | 1.00 |
| With a language hint | 21/22 (2 calls failed with `503`) | 1.00 |

- **Romanized:**
  - Japanese small talk ("Konnichiwa, ogenkidesu ka?") stayed romanized even with the hint
  - Russian small talk was romanized without the hint and fixed with it
- **Misheard words, with or without the hint:**
  - Tamil: "பழக்க" (habit) became "பாரம்பரிய" (traditional), and "Habbit Stacking and Kalina"
  - Chinese: "习惯叠加" became "什么事儿这么急啊"
  - Gemini Live heard the same Tamil clips correctly.
- **Speed:** the median was 7 to 8 s during this run, because Gemini was returning `503 UNAVAILABLE` (model
  overloaded) and the calls were retried. In M0 it was 1.9 s.

### Languages that need special handling

| Language or group | Issue | Where it's handled |
|---|---|---|
| Chinese, and probably Japanese | Live transcripts have spaces between characters | M3: remove spaces between CJK characters |
| Tamil | One agent query not translated. Dictation mishears words. | M3: translate the query on the server if it isn't English. M2: test a stronger transcription model. |
| Japanese, Russian (small talk) | Dictation romanizes short phrases | M2: the re-transcribe retry. M5: measure with real voices. |
| Swahili, and specific terms in Russian, Japanese, Korean and Thai | Domain terms are translated with the wrong English word | M1: give the router the documents' key terms, or search with two English paraphrases |
