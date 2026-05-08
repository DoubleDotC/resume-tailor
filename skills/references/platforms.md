# Platform Scraping Reference

Detailed extraction strategies, selectors, and URL patterns for each supported job platform.
These were derived from hands-on scraping sessions and reflect how the platforms actually behave
as of early 2026.

## Table of Contents

1. [LinkedIn](#linkedin)
2. [Bayt.com](#baytcom)
3. [Jobstreet](#jobstreet)
4. [Glassdoor](#glassdoor)
5. [Indeed](#indeed)

---

## LinkedIn

### Authentication

LinkedIn shows limited results to unauthenticated users and aggressively pushes sign-in modals.
For best results, use an authenticated session. If automating with a browser, have the user log
in first, then reuse that browser profile/session.

**Sign-in modal handling**: LinkedIn overlays a sign-in modal after scrolling or after a few
seconds. The modal has an X/close button that can be clicked to dismiss it. Look for a button
with `aria-label="Dismiss"` or a close icon within the modal overlay. You may need to dismiss
this multiple times during a session.

### Search URL Construction

Base URL: `https://www.linkedin.com/jobs/search/`

Key parameters:
| Parameter | Description | Values |
|-----------|-------------|--------|
| `keywords` | Search query (URL-encoded) | e.g., `data%20analyst` |
| `location` | Location text | e.g., `Malaysia`, `Dubai%2C%20UAE` |
| `f_E` | Experience level (comma-separated) | 1=Internship, 2=Entry, 3=Associate, 4=Mid-Senior, 5=Director, 6=Executive |
| `f_TPR` | Time posted | `r86400`=24h, `r604800`=week, `r2592000`=month |
| `f_JT` | Job type | `F`=Full-time, `P`=Part-time, `C`=Contract, `T`=Temporary |
| `start` | Pagination offset | 0, 25, 50, ... (25 per page) |

**Example**: Mid-senior data analyst jobs in Malaysia, past month, full-time:
```
https://www.linkedin.com/jobs/search/?keywords=data%20analyst&location=Malaysia&f_E=3%2C4&f_TPR=r2592000&f_JT=F
```

### DOM Extraction (Search Results Page)

Job cards appear in a scrollable list. Key selectors:

```
Job card container:     .job-card-container, [data-job-id]
Job title:              .job-card-list__title, h3.base-search-card__title
Company name:           .job-card-container__company-name, h4.base-search-card__subtitle
Location:               .job-card-container__metadata-item, span.job-search-card__location
Job link:               a[href*="/jobs/view/"]
```

**Title deduplication bug**: Text extraction sometimes doubles the title (e.g., "Risk AnalystRisk Analyst").
Fix with regex: `title.replace(/(.{15,}?)\s*\1/, '$1')`

**Pagination**: LinkedIn loads ~25 jobs initially. Scroll to the bottom to trigger loading more,
or use the `start` URL parameter for explicit pagination.

### JD Extraction (Job Detail Page)

URL pattern: `https://www.linkedin.com/jobs/view/{job_id}/`

Description container selectors (try in order):
```
.description__text
.show-more-less-html__markup
[class*="description"]
article[class*="jobs-description"]
```

The description may be truncated behind a "Show more" button. Click it or look for the full
content in the `show-more-less-html__markup` container.

### Extractable Metadata

LinkedIn job cards sometimes show badges: "Top Applicant", "High Match", "Easy Apply".
These are useful signals but not reliably present. Don't depend on them.

---

## Bayt.com

### No Authentication Required

Bayt.com serves full search results to unauthenticated users. It's the most scraper-friendly
platform in this list.

### URL Patterns

Search results: `https://www.bayt.com/en/{country}/jobs/{job-type}-jobs/`

Countries: `uae`, `saudi-arabia`, `qatar`, `bahrain`, `kuwait`, `oman`, `egypt`, `jordan`, `lebanon`

Job types map to URL slugs:
- `data-analyst-jobs`
- `business-analyst-jobs`
- `cybersecurity-analyst-jobs`
- `security-analyst-jobs`
- `risk-analyst-jobs`

Pagination: append `?page=2`, `?page=3`, etc.

### JSON-LD Extraction (Recommended)

This is the best extraction method. Every search results page includes a `<script type="application/ld+json">`
tag containing an `ItemList` schema with clean URLs for every listing:

```json
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "url": "https://www.bayt.com/en/uae/jobs/data-analyst-5444739/"},
    {"@type": "ListItem", "position": 2, "url": "https://www.bayt.com/en/uae/jobs/..."}
  ]
}
```

Parse this JSON to get all job URLs on the page. Typically 30 listings per page.

### DOM Extraction (Fallback)

If JSON-LD parsing isn't available, extract from the DOM:

```
Job card:       [class*="job-listing"], .card-content
Job title:      h2 a, [class*="job-title"]
Company:        [class*="company-name"], .t-secondary
Location:       [class*="location"]
Job link:       h2 a[href*="/jobs/"]
```

### Page Text Extraction

The page text (via accessibility tools or text extraction) returns structured data including
job titles, companies, locations, salary ranges, and posting dates in a readable format.
This is a reliable fallback when DOM queries fail.

### JD Extraction (Job Detail Page)

URL pattern: `https://www.bayt.com/en/{country}/jobs/{job-slug}/`

The job detail page often includes JSON-LD with a `description` field. Otherwise, look for:
```
.card-content [class*="description"]
[class*="job-description"]
```

The description text on Bayt.com tends to include a "Summary" section first (visible on search
results) followed by the full JD on the detail page.

---

## Jobstreet

### Authentication

Jobstreet shows full search results without login, but an authenticated session gets personalized
results and "New to you" badges.

### Search URL Construction

Base URL: `https://my.jobstreet.com/`

Search URL format: `https://my.jobstreet.com/{query-slug}-jobs`
- Spaces become hyphens
- OR operators are supported: `data-analyst-OR-cybersecurity-analyst-jobs`
- Location filter: append `/in-{location}` (e.g., `/in-Kuala-Lumpur`)

Example: `https://my.jobstreet.com/data-analyst-OR-business-analyst-jobs`

### DOM Extraction

Jobstreet is a React app. Key `data-automation` attributes:

```
Job title:      a[data-automation="jobTitle"] or h3 > a
Company:        a[data-automation="jobCompany"] or [data-automation="jobCompany"]
Location:       span[data-automation="jobLocation"] or [data-automation="jobLocation"]
Job card:       article[data-testid] or article
```

**JavaScript execution caveat**: In some automation contexts (particularly MCP-based browser
control), JavaScript execution against Jobstreet pages may be blocked or return sanitized
results. If this happens, fall back to:
1. Accessibility tree parsing (reading the page structure via a11y APIs)
2. Page text extraction
3. Parsing the HTML source directly

### Job Link Pattern

Links follow: `https://my.jobstreet.com/job/{job_id}`

The full URL on the page includes tracking parameters:
`/job/{id}?type=standard&ref=search-standalone&origin=cardTitle#sol={hash}`

Strip everything after the job ID for a clean, stable link.

### JD Extraction (Job Detail Page)

URL: `https://my.jobstreet.com/job/{job_id}`

Description selector: `[data-automation="jobAdDetails"]`

The JD container includes the full posting text, requirements, and benefits.

### Pagination

30 results per page. Pagination via `?pg=2`, `?pg=3` in URL, or by clicking next page buttons
in the pagination nav at the bottom.

---

## Glassdoor

### Authentication Challenges

Glassdoor is the most hostile platform to scrape:
- Blocks many proxy/egress IPs
- Requires authentication for most features
- Shows CAPTCHAs to suspected bots
- Rate-limits aggressively

**Recommendation**: Always use browser automation with an authenticated session. Have the user
log in manually first.

### Search URL

Base: `https://www.glassdoor.com/Job/`

Search results use a complex URL pattern:
```
/Job/{location}-{query}-jobs-SRCH_IL.0,{loc_len}_IC{area_id}_KO{loc_len},{query_len}.htm
```

It's easier to navigate to Glassdoor and use the search form rather than constructing URLs manually.

### DOM Extraction

Glassdoor frequently changes its class names, but patterns include:

```
Job title:      [class*="JobCard_jobTitle"], a[data-test="job-link"]
Company:        [class*="EmployerProfile"], [class*="employer-name"]
Location:       [class*="location"], [class*="JobCard_location"]
Job link:       a[href*="/job-listing/"][href$=".htm"]
```

**Company name gotcha**: Early extraction attempts often miss company names because they're in
a separate sub-element. Target `[class*="EmployerProfile"]` specifically.

### JD Extraction

Job detail pages contain descriptions in:
```
[class*="JobDetails"]
#JobDescriptionContainer
[class*="jobDescriptionContent"]
```

### Link Format

Glassdoor job links end in `.htm` and contain both the job title and company encoded in the URL:
```
/job-listing/data-analyst-accenture-JV_IC2986682_KO0,12_KE13,22.htm
```

These are stable and unique — good for deduplication.

---

## Indeed

### Reliability Warning

Indeed frequently times out (60s+) in browser automation, especially for non-US markets like
`malaysia.indeed.com`. Treat as lowest priority and have graceful fallback when it fails.

### Search URL

Pattern: `https://{country}.indeed.com/jobs?q={query}&l={location}`

Country subdomains: `malaysia.indeed.com`, `ae.indeed.com`, `sa.indeed.com`, `sg.indeed.com`

### DOM Extraction

```
Job card:       .job_seen_beacon, .result
Job title:      h2.jobTitle a, [data-jk] a
Company:        [data-testid="company-name"], .companyName
Location:       [data-testid="text-location"], .companyLocation
Job link:       a[href*="/viewjob"], a[data-jk]
```

### JD Extraction

Job detail URL: `https://{country}.indeed.com/viewjob?jk={job_key}`

Description in: `#jobDescriptionText`

### Pagination

Results per page: ~15. Paginate with `&start=10`, `&start=20`, etc.
