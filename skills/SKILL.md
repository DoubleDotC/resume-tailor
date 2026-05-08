---
name: job-scraper
description: >
  Specification for building a multi-platform job webscraper that finds listings matching a candidate's
  profile and outputs structured data (CSV + full job descriptions) for downstream resume tailoring.
  Use this skill when asked to build, implement, or improve a job scraping tool, job search automation,
  or any pipeline that collects job listings from websites like LinkedIn, Glassdoor, Bayt.com, Jobstreet,
  or Indeed. Also trigger when the user mentions scraping job boards, automating job search, collecting
  job postings, or building a job hunting pipeline — even if they don't say "scraper" explicitly.
---

# Job Scraper — Build Specification

This skill is a blueprint. It describes **what** to build and encodes hard-won knowledge about how each
job platform actually behaves in practice. It does not prescribe a specific architecture — the
implementer should choose the right tools (Playwright, Selenium, requests, browser automation, etc.)
based on the target environment.

## What This Scraper Does

Given a candidate's master resume, the scraper:

1. **Searches** multiple job platforms using targeted queries derived from the resume
2. **Extracts** job metadata (title, company, location, work type, link) from search result pages
3. **Navigates** to each individual job listing and extracts the **full job description text**
4. **Filters** results against configurable exclusion rules (banks, audit roles, nationality restrictions, etc.)
5. **Deduplicates** across platforms by normalizing URLs
6. **Outputs** a CSV of matching jobs plus a folder of full JD text files, ready to pipe into a resume tailoring tool

## Inputs

- **Master resume** — A markdown (or plain text) file describing the candidate's experience, skills, education, and certifications. The scraper uses this to determine which search queries to run and how to categorize results.
- **Configuration** — A config file or CLI flags controlling: target locations, platforms to search, exclusion rules, experience level filters, and output paths.

## Outputs

- **CSV file** with columns: `Category, Job Title, Company, Location, Work Type, Source, Link, Application Status`
  - Sorted by Location → Category → Company
  - `Application Status` is blank (for manual tracking)
- **Job descriptions folder** — One text file per job, named `{source}_{job_id}.txt`, containing the full JD text extracted from the listing page. These files are what get fed into the resume tailoring tool.
- **Metadata JSON** (optional) — A machine-readable version of the CSV for programmatic consumption, including the path to each JD file.

## Architecture Considerations

The scraper needs to handle a spectrum of platform difficulty:

- **Easy**: Bayt.com embeds structured JSON-LD data right in the page. No authentication needed.
- **Medium**: Jobstreet renders job cards in the DOM with `data-automation` attributes. Accessible without login but JavaScript-rendered.
- **Hard**: LinkedIn requires authentication for full results and throws sign-in modals at unauthenticated scrapers. Glassdoor aggressively blocks automated access.

This means the implementation likely needs both **HTTP-based extraction** (for platforms with structured data or public APIs) and **browser automation** (for JS-rendered platforms and those requiring login). A headless browser like Playwright is the most versatile single tool, but consider a hybrid approach where you use lighter methods when possible.

### Rate Limiting and Politeness

Job boards will throttle or block aggressive scrapers. Build in:
- 2-5 second delays between page loads (randomized)
- Retry logic with exponential backoff for timeouts (start at 10s, max 60s)
- Session persistence (reuse cookies/auth across requests within a platform)
- User-agent rotation is helpful but not required if you're running through a real browser

## Platform-Specific Scraping Guide

Read `references/platforms.md` for detailed extraction strategies per platform. The key insights
are summarized here — the reference file has selectors, URL patterns, and code-level details.

### LinkedIn

The richest source but the most hostile to scraping. URL parameters are powerful for filtering:
- `f_E=3,4` — Experience level (3=Associate, 4=Mid-Senior)
- `f_TPR=r2592000` — Posted in last 30 days
- `f_JT=F` — Full-time only
- `keywords=` — URL-encoded search query

**Key challenges**: Sign-in modals that overlay results (must be dismissed), titles that appear doubled in DOM text (e.g., "Data AnalystData Analyst"), and pagination that requires scrolling to load more results.

**JD extraction**: Individual job pages at `linkedin.com/jobs/view/{id}/` contain the full description in a `.description__text` or `[class*="description"]` container.

### Glassdoor

Heavily blocks automated access (proxy restrictions, CAPTCHAs). Browser automation with an authenticated session is basically required.

**Extraction**: Job cards use class names containing `EmployerProfile` for company names and `location` for locations. Links end in `.htm`.

**JD extraction**: Job detail pages have description content in `[class*="JobDetails"]` or `#JobDescriptionContainer`.

### Bayt.com (GCC/Middle East)

The easiest platform to scrape. Pages embed `schema.org/ItemList` JSON-LD in a `<script type="application/ld+json">` tag, giving you clean URLs for every listing on the page without needing to parse the DOM at all.

**URL patterns**: `bayt.com/en/{country}/jobs/{job-type}-jobs/` where country is `uae`, `saudi-arabia`, `qatar`, etc.

**JD extraction**: Individual job pages have description text in the main content area. The JSON-LD on job detail pages often includes `description` field directly.

### Jobstreet (Malaysia/SEA)

JavaScript-rendered React app. DOM elements use `data-automation` attributes: `jobTitle`, `jobCompany`, `jobLocation`. Job links follow `my.jobstreet.com/job/{id}`.

**Key challenge**: JavaScript execution may be blocked or restricted in some automation contexts. Fall back to accessibility tree parsing or page text extraction if DOM queries fail.

**JD extraction**: Job detail pages at `my.jobstreet.com/job/{id}` contain the description in `[data-automation="jobAdDetails"]`.

### Indeed

Frequently times out in browser automation (60s+ with no response). Treat as lowest priority. If implementing, use their search URL pattern: `{country}.indeed.com/jobs?q={query}&l={location}`.

## Search Query Strategy

For each platform, run searches across these category/query pairs:

| Category | Search Queries |
|----------|---------------|
| Data Analyst | `data analyst`, `business intelligence analyst`, `Power BI analyst` |
| Business Analyst | `business analyst`, `strategy analyst` |
| Cybersecurity | `cybersecurity analyst OR SOC analyst OR security analyst`, `information security analyst`, `GRC analyst` |
| Risk Analyst | `risk analyst`, `compliance analyst` |

Use OR operators where the platform supports them (LinkedIn, Jobstreet). On platforms that don't (Bayt.com), run separate searches per term.

Combine queries with each target location. For LinkedIn, set location in URL parameters rather than query text.

## Filtering Rules

Read `references/filtering.md` for the full exclusion list with rationale. The core rules:

### Company Exclusions
Exclude listings from banks and financial institutions (configurable list). Default exclusions:
Maybank, CIMB, Hong Leong, UOB, OCBC, RHB, Alliance Bank, AmBank, Public Bank, Affin Bank,
Bank Islam, Bank Muamalat, Standard Chartered, HSBC, Bank Negara, JPMorgan.

The reason for bank exclusions should be configurable — the current user excludes them for personal/religious reasons, but another user might want to exclude a different industry entirely.

### Title Exclusions
- Roles containing `audit` or `auditor` (unless the user's profile explicitly targets audit)
- Roles containing `intern` or `internship`
- Nationality-restricted roles: `UAE National`, `Emirati`, `Saudi National`, `citizen only`
- Contract/temporary indicators: `FTC`, `contract`, `temp`, `fixed term`
- Entry-level indicators when targeting mid/senior: `fresh graduate`, `entry level` (but not when combined with "senior")

### Deduplication
Normalize URLs before comparing:
- Strip query parameters and fragments
- Remove trailing slashes
- Treat `www.` and non-www as equivalent
- When the same job appears on multiple platforms, keep the one from the platform with the most detail (prefer LinkedIn or the original company posting over aggregator links)

## Profile Matching

This is where the scraper goes beyond keyword matching. The resume is a compressed representation
of skills — a candidate who lists BigQuery on their resume obviously knows SQL. Someone who worked
as a corporate analyst clearly uses Excel, PowerPoint, and Word daily even if those aren't listed.

### Inference Rules

When reading the resume, expand the skill set by inferring:

- **Tool ecosystems**: BigQuery → SQL, GCP; Microsoft Purview → Azure, compliance tools; Power BI → DAX, data modeling
- **Role-implied skills**: Any analyst role → Excel, PowerPoint, Word, business communication; Any cybersecurity role → risk assessment, incident response fundamentals
- **Certification implications**: CompTIA Security+ → network security, threat analysis, compliance frameworks; any cloud cert → cloud architecture basics
- **Education bridging**: Computer Science degree → programming fundamentals, algorithms; Finance/Accounting degree → financial modeling, regulatory awareness

Use a **70% overlap heuristic**: if a job's requirements overlap with 70% of the candidate's inferred skill set, include it. This should be a soft filter — when in doubt, include the listing. It's better to surface a few irrelevant results than to miss good opportunities.

## Integration with Resume Tailoring Tool

The scraper's output is designed to feed into an LLM-based resume tailoring tool. The integration
flow looks like this:

1. Scraper produces CSV + folder of JD text files
2. For each job the candidate wants to apply to, the tailoring tool reads:
   - The master resume
   - The specific JD text file
3. The tailoring tool produces a customized resume

To support this, the JD text files should be:
- Clean plain text (no HTML tags, no navigation chrome, no cookie banners)
- Named predictably: `{source}_{job_id}.txt` (e.g., `linkedin_4387276159.txt`, `bayt_5444739.txt`)
- Stored in a configurable output directory

If the tailoring tool has a CLI interface, the scraper can optionally invoke it directly:
```
for each job in selected_jobs:
    tailoring_tool --resume master_resume.md --jd jd_files/{source}_{id}.txt --output tailored_resumes/
```

## Error Handling

Job scraping is inherently flaky. Platforms change their DOM, rate-limit unpredictably, and
occasionally go down. The scraper should be resilient:

- **Timeout handling**: If a page doesn't load within 30s, skip it and log the failure. Retry once after all other pages are done.
- **Partial results**: If a platform fails entirely (e.g., Indeed times out), continue with the others. Output what you got and report what failed.
- **Stale selectors**: When DOM extraction returns empty results for a platform that should have data, log a warning. This likely means the platform changed its markup.
- **Progress logging**: Print which platform/query/page is being processed so the user knows where it's at and where it failed.

## Configuration

The scraper should accept configuration for:

```yaml
# Target locations
locations:
  - Malaysia
  - UAE
  - Saudi Arabia
  - Qatar
  - Singapore
  - Remote

# Platforms to search (in priority order)
platforms:
  - linkedin
  - bayt
  - jobstreet
  - glassdoor
  - indeed

# Experience level (maps to platform-specific filters)
experience_level: mid_senior  # options: entry, mid, mid_senior, senior

# Employment type
employment_type: full_time

# Exclusion rules
exclude_companies:
  - Maybank
  - CIMB
  # ... etc

exclude_title_keywords:
  - audit
  - intern
  - UAE National
  # ... etc

# Job categories to search
categories:
  data_analyst:
    queries: ["data analyst", "business intelligence analyst", "Power BI analyst"]
  business_analyst:
    queries: ["business analyst", "strategy analyst"]
  cybersecurity:
    queries: ["cybersecurity analyst OR SOC analyst", "security analyst", "GRC analyst"]
  risk_analyst:
    queries: ["risk analyst", "compliance analyst"]

# Output
output_csv: job_listings.csv
output_jd_dir: job_descriptions/
output_metadata: metadata.json

# Resume path (for profile matching)
resume_path: master_resume.md
```
