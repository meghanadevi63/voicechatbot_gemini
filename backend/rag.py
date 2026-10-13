import logging
from functools import lru_cache
from typing import Literal

from google import genai
from google.genai import types
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from backend.config import get_settings
from backend.vectorstore import get_vectorstore

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """You are the intent router for a voice assistant that answers questions about a company's documents.
Classify the user's latest message into exactly one intent:

- "small_talk": the message is ONLY social conversation: greetings (hi, good morning, how are you),
  thanks, congratulations or wishes (happy birthday, well done), compliments, acknowledgements
  (ok, got it), or goodbyes.
- "document_query": anything else. This includes any question or request for information, follow-ups
  such as "tell me more" or "what about the second one?", and messages that mix small talk with a
  question (e.g. "thanks! and what is the leave policy?").

When unsure, choose "document_query".

If the intent is "small_talk", write a warm, natural reply of one or two short sentences in `reply`.
Do not state any facts or answer questions. You may offer to help with questions about the documents.

If the intent is "document_query", write a standalone search query in `search_query`. Use the conversation
history only to resolve references the message can't be understood without ("it", "that", "the second one",
"tell me more"). If the message already makes sense on its own, copy it unchanged (translated into English if
it isn't English). Never add a
document title, book title, author name or other context the user didn't ask about: every search already runs
over the user's documents, and those extra words pull in the wrong passages. Keep the key terms. Leave `reply`
empty.

Languages: the user may write in any language, but the documents are in English.
- `language`: the language of the user's latest message as a BCP-47 code ("en", "hi", "te", "pt-BR", ...).
  Use "und" when the message is too short or too mixed to tell (e.g. "ok", "thanks", a single name).
- `search_query` is always in English. Translate the message if needed, keeping its key terms.
- `alt_query`: if a key term in the message could be translated into English in more than one way (a word
  that means both "habit" and "behavior", or a concept with a well-known English name such as "habit
  stacking"), write a second English search query using the other wording. Otherwise leave it empty.
- `reply` is in the language of the user's latest message, or in the conversation's language when that
  message's language is "und"."""

SYSTEM_PROMPT = """You are a friendly voice assistant that answers questions using the document excerpts provided below.

Rules:
- Answer only from the excerpts. Do not use outside knowledge or guess.
- If the excerpts do not contain the answer, say plainly that you couldn't find it in the documents.
  If they contain part of the answer, give that part and say what is missing.
- Use the conversation history to understand follow-up questions.
- Your reply will be read aloud, so write in natural spoken sentences: no markdown, bullet symbols, tables,
  headings or URLs. When listing a few items, say them in a sentence ("first..., then...").
- Keep it short: two to four sentences, unless the user asks for more detail.
- Don't mention "excerpts", "context" or chunk labels. If it helps, name the source document naturally
  (e.g. "According to the HR policy...").
- Language: {reply_language}, in that language's usual script. The excerpts are always in English whatever
  language you reply in. This applies to every sentence, including saying you couldn't find something. Don't
  add English translations of terms in parentheses.

Document excerpts:
{context}"""

MAX_HISTORY_MESSAGES = 6


class Route(BaseModel):
    """Routing decision for the user's latest message."""

    intent: Literal["small_talk", "document_query"] = Field(
        description="small_talk for purely social messages, document_query for everything else"
    )
    language: str = Field(
        default="und", description='BCP-47 code of the latest message\'s language, or "und" if unclear'
    )
    reply: str = Field(default="", description="Short reply, only when intent is small_talk")
    search_query: str = Field(
        default="", description="Standalone English search query, only when intent is document_query"
    )
    alt_query: str = Field(
        default="", description="Optional second English query with different wording for a key term"
    )


def format_docs(docs: list[Document]) -> str:
    return "\n\n".join(
        f"[{d.metadata.get('source')} p.{d.metadata.get('page')}]\n{d.page_content}"
        for d in docs
    )


def to_sources(docs: list[Document]) -> list[dict]:
    return [
        {
            "source": d.metadata.get("source"),
            "page": d.metadata.get("page"),
            "content": d.page_content,
        }
        for d in docs
    ]


def to_messages(history: list[dict]) -> list:
    messages = []
    for turn in history[-MAX_HISTORY_MESSAGES:]:
        cls = HumanMessage if turn["role"] == "user" else AIMessage
        messages.append(cls(content=turn["content"]))
    return messages


def is_known(language: str | None) -> bool:
    return bool(language) and language.lower() not in ("und", "auto")


def reply_language(detected: str, conversation: str | None) -> str:
    """The language instruction for the answer prompt."""
    if is_known(detected):
        return f"reply in the same language as the user's latest message (BCP-47 code {detected})"
    if is_known(conversation):
        return f"reply in the conversation's language (BCP-47 code {conversation})"
    return "reply in the same language the user's latest message is written in (English if it is in English)"


@lru_cache
def get_gemini() -> genai.Client:
    return genai.Client(api_key=get_settings().gemini_api_key)


class RAGChain:
    def __init__(self):
        settings = get_settings()
        self.vectorstore = get_vectorstore()

        router_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", ROUTER_PROMPT),
                MessagesPlaceholder("history"),
                ("human", "{question}"),
            ]
        )
        router_llm = ChatGroq(
            model=settings.router_model, api_key=settings.groq_api_key, temperature=0
        )
        self.router = router_prompt | router_llm.with_structured_output(Route)

        # Groq answer chain: the answer writer when ANSWER_PROVIDER=groq, and the fallback when Gemini fails
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT),
                MessagesPlaceholder("history"),
                ("human", "{question}"),
            ]
        )
        llm = ChatGroq(model=settings.groq_model, api_key=settings.groq_api_key, temperature=0)
        self.chain = prompt | llm | StrOutputParser()

    def refresh(self):
        """Rebuild the vector store after re-ingestion (collection was dropped and recreated)."""
        self.vectorstore = get_vectorstore()

    def retrieve(self, query: str, alt_query: str = "") -> list[Document]:
        """Top-k excerpts for the query. With alt_query, both are searched and the best matches merged."""
        k = get_settings().top_k
        best: dict[tuple, tuple[float, Document]] = {}
        for q in filter(None, (query.strip(), alt_query.strip())):
            for doc, score in self.vectorstore.similarity_search_with_score(q, k=k):
                key = (doc.metadata.get("source"), doc.metadata.get("page"), doc.page_content)
                if key not in best or score > best[key][0]:
                    best[key] = (score, doc)
        ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)
        return [doc for _, doc in ranked[:k]]

    def route(self, question: str, messages: list) -> Route:
        try:
            return self.router.invoke({"history": messages, "question": question})
        except Exception:
            # Never block an answer on the router: fall back to normal retrieval.
            logger.exception("Intent router failed; falling back to retrieval")
            return Route(intent="document_query", search_query=question)

    def write_answer(self, question: str, history: list[dict], context: str, language: str) -> str:
        settings = get_settings()
        values = {"context": context, "reply_language": language}
        if settings.answer_provider == "gemini":
            try:
                return self.gemini_answer(question, history, SYSTEM_PROMPT.format(**values))
            except Exception:
                logger.exception("Gemini answer failed; falling back to Groq")
        return self.chain.invoke({**values, "history": to_messages(history), "question": question})

    def gemini_answer(self, question: str, history: list[dict], system: str) -> str:
        settings = get_settings()
        contents = [
            types.Content(role="user" if t["role"] == "user" else "model", parts=[types.Part(text=t["content"])])
            for t in history[-MAX_HISTORY_MESSAGES:]
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
        response = get_gemini().models.generate_content(
            model=settings.answer_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0,
                # Minimal thinking keeps the answer at ~1-2 s
                thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            ),
        )
        answer = (response.text or "").strip()
        if not answer:
            raise ValueError("Empty answer from Gemini")
        return answer

    def answer(self, question: str, history: list[dict], language: str | None = None) -> dict:
        """language: the conversation's language (BCP-47), used when the question's own language is unclear."""
        messages = to_messages(history)
        route = self.route(question, messages)
        detected = route.language if is_known(route.language) else (language if is_known(language) else "und")
        logger.info(
            "Routed %r -> %s (language=%s, query=%r, alt=%r)",
            question, route.intent, route.language, route.search_query, route.alt_query,
        )

        if route.intent == "small_talk" and route.reply.strip():
            return {"answer": route.reply.strip(), "sources": [], "language": detected}

        docs = self.retrieve(route.search_query.strip() or question, route.alt_query)
        answer = self.write_answer(question, history, format_docs(docs), reply_language(route.language, language))
        return {"answer": answer, "sources": to_sources(docs), "language": detected}
