# Smart Application Questions (all Workday tenants)

## Files to copy into `agent/flows/`

| File | Role |
|------|------|
| `question_class.py` | Classify question **text** → class (WORK_AUTH, COMPLIANCE_NO, …) |
| `answer_ladder.py` | Profile → rules → learned → 3-LLM → human |
| `workday_smart.py` | Find controls on page, bind to labels, apply ladder |
| `workday.py` | Wired to call smart fill on Application Questions |

Also ensure `candidate.md` has a solid `## screening_defaults` block (already present).

Optional: `agent/out/learned_answers.json` starts empty and fills after successful answers.

## Decision order

1. **Profile / screening_defaults** (`candidate.md`)
2. **Class rules** (e.g. compliance → No, work auth → Yes, sponsorship → No)
3. **Learned store** (successful answers from prior jobs)
4. **3-LLM panel** (quorum of 2, real options only)
5. **Telegram** (only if still required and unresolved)

Recorded choices are **never** applied to generic `Select One` / `Yes Required` controls.

## Classes

- `WORK_AUTH` — legally eligible / authorized to work  
- `SPONSORSHIP` — need visa sponsorship  
- `COMPLIANCE_NO` — prior employee, related to staff, non-compete, family HCP, etc.  
- `COMPLIANCE_YES` — agree to terms, willing to travel (when profile says so)  
- `HOW_HEARD` — LinkedIn / Job Board from profile  
- `EEO_SENSITIVE` — gender/race/veteran/disability — **profile or human only, no model**  
- `CONDITIONAL_DETAIL` — “if you answered yes…” → N/A when parent is No  
- `UNKNOWN` → panel / human  

## Rebuild

```powershell
cd "C:\Python SW\Linkedin Scraper\jobhuntwow-app\agent"
# copy the four py files into flows\
docker compose -f docker-compose.local.yml up -d --build jhw-agent
```

## Expected log

```text
[wd-smart] indexed N question block(s)
[ladder] Q class=WORK_AUTH ... 
[ladder] source=profile -> 'Yes'
[ladder] Q class=COMPLIANCE_NO ...
[ladder] source=rule -> 'No'
[wd-smart] select <- 'No' (rule) | 'Have you previously been considered...'
```
