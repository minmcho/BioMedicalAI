# How to become an Anthropic Forward Deployed Engineer (Applied AI) — Min's roadmap

Role facts (from public postings, captured Oct 2026 — verify the live posting before applying):
- Team: Applied AI. FDEs embed with strategic customers to build production applications with Claude inside customer systems.
- Deliverables named in the posting: MCP servers, sub-agents, and agent skills that customers run in production; repeatable deployment patterns fed back to Product/Engineering.
- Working split reported for the role: ~40% building, ~30% customer-facing, ~30% internal feedback.
- Requirements (US posting): 4+ years in a technical customer-facing role; production LLM experience — advanced prompt engineering, agent development, evaluation frameworks, deployment at scale; strong Python (TypeScript/Java a plus); shipped production applications.
- Travel ~25%. Posted band reported at $200k-$300k — clears the $10k/mo after-tax floor (see Bootcamp/tracker/COMP_TARGET.md).
- Official posting: https://job-boards.greenhouse.io/anthropic/jobs/4985877008 and https://www.anthropic.com/careers

## Honest gap map (Min, Oct 2026)
| Requirement | Status | Evidence / fix |
|---|---|---|
| Python + shipped production apps | STRONG | Agentic_App, LIMS systems, BioMedicalAI |
| MCP servers / tools | STRONG START | Agentic_App has MCP tool creation; fix = ship a public Claude MCP server (Phase B) |
| Agent development + eval frameworks | PARTIAL | RAG + eval harness exist, but on local/Ollama models; fix = rebuild one on Claude API with graders |
| Production LLM at an external customer | MISSING | The core gap in 9/12 FDE postings; fix = Phase C pilot with real users + case study |
| Customer-facing / consulting record | PARTIAL | 18 yrs requirements, training, support for scientists — internal customers; reframe honestly, add discovery artifacts |
| Claude-specific ecosystem | MISSING | Fix = Phase A Academy courses + Claude Code daily use |
| Values / responsible AI | STRONG STORY | Regulated lab work, privacy-first Mindglen design; prepare explicit stories for the values round |

## Phase A (Weeks 1-3) — Anthropic Academy, free, in order
All at https://anthropic.skilljar.com/
1. Claude 101 — https://anthropic.skilljar.com/claude-101
2. Building with the Claude API — https://anthropic.skilljar.com/claude-with-the-anthropic-api
3. Introduction to Model Context Protocol — https://anthropic.skilljar.com/introduction-to-model-context-protocol
4. MCP: Advanced Topics — https://anthropic.skilljar.com/model-context-protocol-advanced-topics
5. Introduction to Agent Skills — https://anthropic.skilljar.com/introduction-to-agent-skills
6. Claude Code in Action — https://anthropic.skilljar.com/claude-code-in-action
Optional for his AWS background: Claude with Amazon Bedrock.
Also: use Claude Code as the daily driver during the bootcamp — genuine product fluency is visible in interviews.
Optional credential: Claude Certified Architect (launched 2026, via Claude Partner Network) — prep is the Academy courses above; do it only after Phase B ships.

## Phase B (Weeks 3-7) — Build ONE Claude deployment, the Anthropic way
Project: a biomedical/lab knowledge agent in this repo (ForwardDeployment/03-anthropic-fde/project/):
- Claude API: tool use, structured outputs, long-context document analysis
- A public MCP server exposing lab/biomedical data tools (with auth + audit notes)
- Sub-agents: literature retrieval, data lookup, report drafting — with context management
- Evaluation: a graded eval set (exact/contains/LLM-judge + human review sample), regression tracking, cost/latency logs, failure/transcript analysis
- Safety: no diagnosis/treatment outputs; hedged, source-cited answers; sensitive-data handling documented
Done = public repo + live demo + README with eval numbers.

## Phase C (Weeks 7-10) — Become customer-shaped
- Run a real pilot: put the Phase B agent in front of actual users (Agentic_App beta users / lab colleagues) and write the deployment case study: discovery, scope, what broke, eval improvement, adoption.
- Write the repeatable deployment pattern as a playbook doc — this is literally an FDE deliverable.
- One public artifact: blog-style write-up or talk notes.

## Phase D (Weeks 10-12) — Interview loop prep
Reported loop: recruiter screen, real coding (Python), a deployment case (discovery -> scope -> eval -> production), transcript/LLM-system analysis, and a values round that eliminates strong coders — prepare for all five.
- Coding: Python fluency drills, API/data wrangling under time.
- Case: rehearse the Phase C case study end-to-end in 20 minutes.
- Values: responsible-AI stories from regulated lab + privacy-first product work; be specific, not slogans.

## Application timing
Apply when the Phase B demo is live (~Week 6-7), not at Week 12. Apply to the SF seat. In parallel, watch Anthropic's enterprise-services venture and large implementation partners as alternate doors into the same work.
