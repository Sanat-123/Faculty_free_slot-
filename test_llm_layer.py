"""Tests for engine/llm_layer.py using a scripted fake Claude client (no API key needed)."""
import sys, os, types
sys.path.insert(0,'.')
import pdf_pipeline as pp
from engine.llm_layer import LLMAssistant

_pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
if not _pdf:
    sys.exit("no timetable PDF: pass one as argument or put one in the data folder")
data = pp.parse_faculty_pdf(open(_pdf,"rb").read())
dbp, _ = pp.build_database(data)

# 1) no key -> rule-based mode, nothing breaks
os.environ.pop("ANTHROPIC_API_KEY",None)
pp.activate(dbp)
assert not pp.ai_enabled()
r=pp.answer_query("Who is free on Monday slot 3?")
assert r["intent"]=="FREE_LIST", r["intent"]
print("1 ok: no key -> rule engine,", r["intent"])

# 2) fake Claude: asks the tool, then writes a final answer
class B:  # content block
    def __init__(s,**k): s.__dict__.update(k)
class Resp:
    def __init__(s,content,stop): s.content=content; s.stop_reason=stop
class FakeClient:
    def __init__(s): s.calls=[]; s.messages=s
    def create(s,**kw):
        kw=dict(kw); kw["messages"]=list(kw["messages"]); s.calls.append(kw)
        msgs=kw["messages"]
        last=msgs[-1]
        if last["role"]=="user" and isinstance(last["content"],str):
            return Resp([B(type="text",text="Let me check."),
                         B(type="tool_use",id="t1",name="ask_timetable",
                           input={"question":"Is Dr. Mehul Mahrishi free on Monday slot 3?"})],"tool_use")
        tr=last["content"][0]["content"]
        return Resp([B(type="text",text="Short answer: no. Details:\n"+tr)],"end_turn")

pp.activate(dbp)
fake=FakeClient()
pp._LLM=LLMAssistant(pp._SMART_ENGINE, client=fake)
hist=[{"role":"assistant","content":"hi"},{"role":"user","content":"hello"},{"role":"assistant","content":"Hello!"},
      {"role":"user","content":"who teaches OS III"},{"role":"assistant","content":"4 teachers"}]
r=pp.answer_query("is he free monday 3rd period?", history=hist)
assert r["intent"]=="AI_ASSISTANT"
assert "Mehul" in r["text"] and "OS III" in r["text"], r["text"]
assert r["entities"]["Asked the timetable"]==["Is Dr. Mehul Mahrishi free on Monday slot 3?"]
sent=fake.calls[0]["messages"]
assert sent[0]["role"]=="user" and all(a["role"]!=b["role"] for a,b in zip(sent,sent[1:])), [m["role"] for m in sent]
assert sent[-1]["content"]=="is he free monday 3rd period?"
assert "Faculty (81)" in fake.calls[0]["system"]
print("2 ok: tool loop, grounded text, history alternation; roles:",[m["role"] for m in sent])

# 3) API failure -> silent fallback to rule engine
class Boom:
    messages=None
    def __init__(s): s.messages=s
    def create(s,**k): raise RuntimeError("network down")
pp._LLM=LLMAssistant(pp._SMART_ENGINE, client=Boom())
r=pp.answer_query("Who is free on Monday slot 3?")
assert r["intent"]=="FREE_LIST"
print("3 ok: API error -> fallback", r["intent"])

# 4) key present but SDK/off switch
os.environ["ANTHROPIC_API_KEY"]="sk-test"; os.environ["FACULTY_LLM_OFF"]="1"
assert not LLMAssistant.available()
os.environ.pop("FACULTY_LLM_OFF"); assert LLMAssistant.available()
pp.activate(dbp); assert pp.ai_enabled()
print("4 ok: key enables AI layer, FACULTY_LLM_OFF=1 disables it")