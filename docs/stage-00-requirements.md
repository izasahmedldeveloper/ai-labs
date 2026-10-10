# Stage 0 — Requirements & capacity estimation

## Why this comes first
Every later choice (cache? queue? small model?) is justified by numbers here.
Without them you are cargo-culting the video.

## Functional requirements (what it does)
- Students upload course notes (PDF/markdown); ask questions answered from those notes, with citations.
- Instructors can upload/replace notes; students cannot.
- Questions the bot can't answer confidently or that are sensitive (e.g. grade disputes) go to a human tutor.

## Non-functional requirements (how well) — *your targets, edit them*
| Quality | Starting target | Why it matters |
|---|---|---|
| P95 latency to first token | < 1.5 s (full answer < 6 s) | chat feels broken beyond this |
| Availability | 99.5% | study tool, not payments |
| Cost ceiling | $50 / month | forces caching + routing |
| PII leakage | 0 student names/emails in answers or logs | trust/compliance |
| Answer groundedness | ≥ 90% answers supported by cited notes | core quality metric |

## Back-of-the-envelope (assumptions — change them!)
- 2,000 students, 30% active daily → 600 DAU, ~8 questions each → **4,800 req/day**
- Traffic clusters before exams: peak ≈ 10× average hour. Avg ≈ 0.055 QPS → **peak ≈ 0.6–1 QPS** (exam eve maybe 3 QPS)
- Tokens/request ≈ 1,500 prompt (question + 4 chunks) + 300 output
- Monthly tokens ≈ 4,800 × 30 × 1,800 ≈ **260 M tokens/month**
- Cost at a $3/M blended frontier price ≈ **$780/month** → 15× over the $50 ceiling.

**Insight:** the budget is impossible with a naive design. That gap is the justification for
semantic caching (Stage 5, FAQ-style repeats maybe 30–40%), routing to small models (Stage 7),
and token budgets (Stage 3). We will re-measure after each stage.

## Decisions for you
1. Are the targets above right for *your* imagined users? Change any you disagree with.
2. Is "grade dispute → human" the right high-stakes example, or something else?
3. Which hurts more for a study tool: a slow correct answer or a fast wrong one? (This sets routing and cache aggressiveness later.)
