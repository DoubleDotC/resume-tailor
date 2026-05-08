# Filtering & Profile Matching Reference

## Exclusion Rules

### Company Exclusions

The default bank exclusion list covers major institutions in Malaysia, GCC, and Singapore.
This list exists because the current user excludes banks for personal reasons, but the
implementation should make it a configurable list — not a hardcoded assumption.

**Default bank list** (case-insensitive partial match against company name):
```
Maybank, CIMB, Hong Leong Bank, UOB, OCBC, RHB Bank, Alliance Bank,
AmBank, Public Bank, Affin Bank, Bank Islam, Bank Muamalat,
Standard Chartered, HSBC, Bank Negara, JPMorgan, Goldman Sachs,
Citibank, Deutsche Bank, Barclays, Credit Suisse
```

**Matching logic**: Use substring matching, not exact match. "RHB" should catch "RHB Bank",
"RHB Banking Group", and "RHB Investment Bank".

### Title-Based Exclusions

These filter on the job title string. All checks are case-insensitive.

| Pattern | Rationale |
|---------|-----------|
| `audit`, `auditor` | User explicitly excludes audit roles |
| `intern`, `internship` | Not targeting internships |
| `UAE National`, `Emirati Talent`, `Emirati National` | Nationality-restricted; user is not UAE national |
| `Saudi National`, `Saudi citizen` | Nationality-restricted; user is not Saudi national |
| `citizen only`, `nationals only` | Generic nationality restriction |
| `FTC`, `fixed term`, `contract` (in title) | User wants permanent roles only |
| `temporary`, `temp ` | Temporary positions |
| `fresh graduate` | User has ~3 years experience, not targeting fresh grad roles |

**Edge cases to handle carefully**:
- "Entry Level" in title: exclude UNLESS the title also contains "Senior" (e.g., "Senior Entry Level Analyst" is unlikely but don't over-filter)
- "Contract" in company name vs title: only exclude when "contract" appears in the job title, not the company name (e.g., "ABC Contracting LLC" is fine)
- "Associate" is a valid level — don't exclude it (it maps to ~2-5 years experience in most companies)
- "DLP Specialist" or "pure DLP" roles: the user has DLP experience but doesn't want to be pigeonholed into pure DLP roles. Filter titles that are exactly "DLP Specialist" or "DLP Engineer" but keep roles where DLP is mentioned alongside other responsibilities

### Work Type Exclusions

- Part-time: exclude by default
- Freelance: exclude
- Volunteer: exclude

### Salary-Based Filtering (Optional)

If salary data is available (Bayt.com and Jobstreet often show ranges), consider flagging
suspiciously low salaries but don't auto-exclude — salary ranges on job boards are often
inaccurate or represent a wider band than the actual offer.

## Deduplication Strategy

Jobs frequently appear on multiple platforms. The same "Data Analyst at Amazon MENA" listing
might show up on LinkedIn, Glassdoor, and Bayt.com.

### URL Normalization

Before comparing, normalize all URLs:
1. Strip query parameters: `?ref=search&origin=card` → remove
2. Strip fragments: `#sol=abc123` → remove
3. Remove trailing slashes
4. Lowercase the entire URL
5. Normalize `www.` prefix (treat `www.linkedin.com` and `linkedin.com` as same)

### Cross-Platform Dedup

URL normalization catches duplicates within a platform, but the same job on different platforms
has completely different URLs. To catch cross-platform duplicates:

1. **Fuzzy title + company match**: If the same company has a listing with a very similar title
   (>85% string similarity after lowercasing) in the same location, it's likely a duplicate.
2. **Keep the richer source**: When deduplicating, prefer the source with more detail.
   Priority order: LinkedIn (most detail, apply tracking) > original company site > Bayt.com >
   Glassdoor > Jobstreet > Indeed.
3. **Log dedup decisions**: When removing a duplicate, log which listing was kept and which was
   removed, so the user can audit the decision.

### Known Duplicate Patterns

From real scraping sessions:
- Bayut/dubizzle listings appear on both LinkedIn and Bayt.com with slightly different titles
  ("Associate Data Analyst - Strategy" vs "Associate Data Analyst-Strategy")
- Al Futtaim Group posts extensively on both LinkedIn and Bayt.com
- Amazon MENA jobs appear across all platforms
- Delivery Hero / HungerStation listings appear under different brand names

## Profile Matching

### Skill Inference from Resume

The resume is a compressed signal. Here's how to expand it:

**Direct tool mappings**:
```
BigQuery         → SQL, GCP, cloud data warehousing
Microsoft Purview → Azure, compliance tools, DLP, information protection
Google Chronicle  → SIEM, security monitoring, log analysis
Power BI         → DAX, data modeling, data visualization
Tableau          → data visualization, dashboard design
Python           → pandas, scripting, automation
SQL              → database querying, data manipulation
GCP              → cloud computing, BigQuery, cloud infrastructure
```

**Role-implied skills** (infer from job titles/descriptions in resume):
```
Data Analyst role        → Excel (advanced), PowerPoint, Word, SQL, reporting,
                           stakeholder communication, business acumen
Cybersecurity role       → risk assessment, incident response, security monitoring,
                           vulnerability assessment, compliance frameworks
Accounting/Finance degree → financial modeling, reconciliation, regulatory awareness,
                           attention to detail, numerical reasoning
Computer Science degree  → programming, algorithms, data structures, problem-solving
```

**Certification expansions**:
```
CompTIA Security+   → network security, threat analysis, risk management,
                      compliance (NIST, ISO 27001), cryptography basics
```

**Language skills** (often underweighted by keyword matchers):
```
Trilingual (English/French/Arabic) → MENA market fit, international team communication,
                                      translation/localization capability
```

### Matching Algorithm

For each job listing's requirements:
1. Extract required skills/qualifications from the JD
2. Compare against the expanded skill set (resume + inferred skills)
3. Calculate overlap percentage
4. Include if overlap >= 70%

This should be a **soft filter** with human override. When in doubt, include the job and let
the user decide. Missing a good opportunity is worse than including a few irrelevant ones.

### Category Assignment

Assign each job to exactly one category based on the primary focus of the role:

| Category | Signals in Title/JD |
|----------|-------------------|
| Data Analyst | data analyst, BI analyst, data scientist, Power BI, Tableau, analytics |
| Business Analyst | business analyst, strategy analyst, systems analyst, process improvement |
| Cybersecurity | SOC analyst, security analyst, cybersecurity, SIEM, penetration testing, GRC, infosec |
| Risk Analyst | risk analyst, compliance analyst, risk management, GRC (when risk-focused) |

When a role spans categories (e.g., "Data & Security Analyst"), assign to the category that
best matches the user's primary skill set. If the JD is equally split, assign to the more
specialized category (Cybersecurity > Data Analyst in this case, since cybersecurity roles
are harder to find).
