# Problem B — NLP-to-SQL Engine with Semantic Layer

## Overview

This system converts plain-English questions into correct SQL queries against the **Cashflo AP database** — a 12-table SQLite database modelling a B2B Accounts Payable platform. It uses a semantic layer for business context, Claude (Anthropic) as the LLM backbone, rule-based ambiguity detection, and a self-correction retry loop for invalid SQL.

---

## Flow Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                     Problem B — NLP-to-SQL Engine                            │
└──────────────────────────────────────────────────────────────────────────────┘

  User types plain-English question
              │
              ▼
  ┌───────────────────────┐
  │   Session History     │  (last 10 turns — multi-turn conversation support)
  └───────────┬───────────┘
              │
              ▼
  ┌───────────────────────┐     cache hit
  │     Query Cache       │──────────────────────────────────────────────┐
  │  (trigram similarity) │                                              │
  └───────────┬───────────┘                                              │
         cache miss                                                      │
              │                                                          │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │  Ambiguity Detector   │  Pattern-based rules (Python)                │
  │  (rule-based backstop)│  "top vendors", "best products", etc.        │
  │                       │  → sets is_ambiguous=true + options list     │
  └───────────┬───────────┘                                              │
              │                                                          │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │   Semantic Layer      │  semantic_layer.yaml                         │
  │   Resolution          │  • Synonyms: bills→invoices, unpaid→filter   │
  │                       │  • Metrics: revenue, outstanding_amount      │
  │                       │  • Join paths, temporal expressions          │
  └───────────┬───────────┘                                              │
              │                                                          │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │  Claude 3.5 Haiku     │  System prompt =                             │
  │  (Anthropic LLM)      │    Schema DDL (auto-discovered)              │
  │                       │    + Semantic layer context                  │
  │                       │    + Conversation history                    │
  │                       │  Returns JSON:                               │
  │                       │    sql, explanation, assumptions,            │
  │                       │    is_ambiguous, clarification_question,     │
  │                       │    clarification_options                     │
  └───────────┬───────────┘                                              │
              │                                                          │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │   SQL Validator       │  Check: all referenced tables exist          │
  └───────────┬───────────┘                                              │
              │ validation error                                         │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │ Self-Correction Retry │  Feed error + valid table list back          │
  │ (1 extra Claude call) │  to Claude → Claude rewrites SQL             │
  └───────────┬───────────┘                                              │
              │ success                                                  │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │   SQLite Executor     │  In-memory DB (loaded from .sql at startup)  │
  └───────────┬───────────┘                                              │
              │                                                          │
              ▼                                                          │
  ┌───────────────────────┐                                              │
  │  Visualization        │  Heuristic: scalar / bar / line / pie /      │
  │  Suggester            │  table based on result shape                 │
  └───────────┬───────────┘                                              │
              │                                                   ◄──────┘
              ▼
  ┌───────────────────────────────────────────────────────────┐
  │  Web UI Response                                          │
  │  • Plain-English explanation                              │
  │  • SQL (collapsible)                                      │
  │  • Result table                                           │
  │  • Chart (if applicable)                                  │
  │  • Ambiguity popup with clickable clarification buttos    │
  │  • Cache badge (if cache hit)                             │
  └───────────────────────────────────────────────────────────┘
```

---

## File Structure

```
Problem_B/
├── app.py                  ← FastAPI web server + all API endpoints
├── main.py                 ← CLI REPL + single-question mode
├── sql_generator.py        ← NLP-to-SQL pipeline (Claude + retry + ambiguity)
├── schema_discovery.py     ← Auto-discovers schema DDL, FKs, sample values
├── query_cache.py          ← Trigram-similarity query cache
├── db.py                   ← In-memory SQLite loader (from .sql file at startup)
├── semantic_layer.yaml     ← Business-friendly schema: synonyms, metrics, joins
├── config.yaml             ← API key, model, cache settings
├── requirements.txt
├── cashflo_sample_schema_and_data (1).sql  ← Official DB dump (sole source of truth)
├── frontend/
│   ├── index.html          ← Chat-style SPA (Tailwind + Alpine.js + Chart.js)
│   └── logo.png
└── output/
    └── query_cache.json    ← Persisted query cache
```

> **No `.db` file on disk.** The database is loaded from the `.sql` dump into an in-memory SQLite connection at server startup. Nothing is written to disk.

---

## Quick Start

### 1. Install dependencies
```bash
cd Problem_B
pip install -r requirements.txt
```

### 2. Configure `config.yaml`
```yaml
llm:
  api_key: "sk-ant-your-key-here"
  model: "claude-3-5-haiku-20241022"   # cheap + capable; swap to sonnet for best accuracy
```

### 3. Launch the web UI
```bash
cd Problem_B
uvicorn app:app --reload --port 8001
```
Open **http://localhost:8001**

### 4. CLI — interactive REPL
```bash
python main.py
```

### 5. CLI — single question
```bash
python main.py --question "Which vendors have overdue invoices?"
```

### 6. Print schema summary
```bash
python main.py --schema
```

---

## Model Selection & Cost

| Model | Input | Output | Per query (1 call) | Per query (with retry) |
|-------|-------|--------|-------------------|----------------------|
| `claude-3-haiku-20240307` | $0.25/M | $1.25/M | ~$0.001 | ~$0.002 |
| **`claude-3-5-haiku-20241022`** ← default | **$0.80/M** | **$4/M** | **~$0.004** | **~$0.008** |
| `claude-3-5-sonnet-20241022` | $3/M | $15/M | ~$0.015 | ~$0.030 |

Change the model in `config.yaml` — no code changes needed.

---

## Database

Loaded from `cashflo_sample_schema_and_data (1).sql` into in-memory SQLite at startup.

| Table | Rows | Key Relationships |
|-------|------|-------------------|
| `companies` | 2 | Buyer entities |
| `departments` | 5 | FK → companies |
| `vendors` | 15 | Supplier master; watchlist flag & rating (A=best, D=worst) |
| `products` | 12 | Item catalog with HSN codes |
| `purchase_orders` | 80 | FK → companies, vendors, departments |
| `po_line_items` | 218 | FK → purchase_orders, products |
| `grns` | 68 | FK → purchase_orders, vendors |
| `grn_line_items` | 188 | FK → grns, products, po_line_items |
| `invoices` | 101 | FK → vendors, companies, purchase_orders |
| `invoice_line_items` | 281 | FK → invoices, products (CGST/SGST/IGST) |
| `payments` | 38 | FK → invoices |
| `approval_matrix` | 4 | Amount-based approval tier lookup |

---

## Key Features

### Ambiguity Handling
When a question is genuinely ambiguous (e.g. "top vendors" — by value? count? rating?), the system shows a popup with **clickable clarification buttons**. Clicking one auto-submits the refined question.

Two-layer detection:
1. **Claude** — flags `is_ambiguous: true` and suggests options in its JSON response
2. **Python backstop** — regex rules guarantee the popup for known patterns regardless of LLM response

Known ambiguous patterns:
- "top/best vendors" → options: by total value / invoice count / rating / on-time rate
- "top/most popular products" → by invoiced value / quantity / invoice count
- "best/biggest customers" → by invoice value / count / payment rate
- "most expensive invoices" → individual vs. vendor total
- "top departments" → by spend / PO count / budget utilisation

### Self-Correction Retry
If Claude generates SQL with a non-existent table (e.g. `vendor_payment_stats`):
1. Validator catches the unknown table
2. Error + list of valid tables is sent back to Claude in the same conversation thread
3. Claude rewrites the SQL using only real tables
4. If second attempt also fails → error is shown to user

### Vendor Rating Sort Order
`vendors.rating` is a letter grade: **A = best, D = worst**.
- "top/best rated" → `ORDER BY rating ASC` (A first — correct)
- "worst rated" → `ORDER BY rating DESC` (D first)
Alphabetical sort = correct grade order. The system prompt explicitly enforces this.

### Query Caching
- Trigram (3-character n-gram) Jaccard similarity
- Threshold: 0.75 (configurable in `config.yaml`)
- Cache stores: SQL, explanation, columns, rows, viz type, ambiguity state, clarification options
- Cache hit returns instantly — zero LLM cost

### Multi-Turn Conversations
Last 10 conversation turns are passed as context with every request. Supports follow-ups like "now break that down by department" without repeating context.

---

## Semantic Layer

The `semantic_layer.yaml` defines:

### Synonyms
| User says | Resolves to |
|-----------|-------------|
| `bills` | `invoices` |
| `unpaid` | `status IN ('received','validated','approved','on_hold')` |
| `overdue` | `due_date < DATE('now') AND status NOT IN ('paid','rejected')` |
| `outstanding` | `status IN ('received','validated','approved','on_hold')` |
| `supplier` | `vendors` |
| `watchlist` | `is_watchlist = 1` |
| `revenue` | metric → SUM(grand_total) WHERE status='paid' |

### Business Metrics
| Metric | SQL |
|--------|-----|
| `revenue` | `SUM(invoices.grand_total) WHERE status = 'paid'` |
| `outstanding_amount` | `SUM(grand_total) WHERE status IN (...)` |
| `overdue_amount` | `SUM(grand_total) WHERE due_date < today AND unpaid` |
| `on_time_payment_rate` | `% invoices paid on or before due_date` |

### Temporal Expressions
- `last month` → `strftime('%Y-%m', DATE('now','-1 month'))`
- `this quarter` → computed 3-month window
- `this year` / `this FY` → April–March for Indian financial year

---

## Sample Questions & Expected SQL

### Simple
```sql
-- "List all vendors on the watchlist."
SELECT name, city, rating FROM vendors WHERE is_watchlist = 1
```

### Joins
```sql
-- "Show all invoices for the Engineering department."
SELECT i.invoice_number, i.grand_total, i.status
FROM invoices i
JOIN purchase_orders po ON po.id = i.po_id
JOIN departments d ON d.id = po.department_id
WHERE d.name = 'Engineering'
```

### Aggregation
```sql
-- "Which product has the highest total invoiced value?"
SELECT p.name, ROUND(SUM(ili.total_amount),2) AS total_invoiced
FROM invoice_line_items ili
JOIN products p ON p.id = ili.product_id
GROUP BY p.id ORDER BY total_invoiced DESC LIMIT 1
```

### Window Functions
```sql
-- "Rank vendors by total invoice value."
SELECT v.name, ROUND(SUM(i.grand_total),2) AS total,
       RANK() OVER (ORDER BY SUM(i.grand_total) DESC) AS rank
FROM invoices i JOIN vendors v ON v.id = i.vendor_id
GROUP BY v.id

-- "Show each invoice alongside the previous invoice amount for the same vendor."
SELECT invoice_number, grand_total,
       LAG(grand_total) OVER (PARTITION BY vendor_id ORDER BY invoice_date) AS prev_amount
FROM invoices
```

### Synonyms & Business Metrics
```sql
-- "What was our revenue last quarter?"
SELECT ROUND(SUM(grand_total),2) AS revenue
FROM invoices
WHERE status = 'paid'
  AND invoice_date >= [last quarter start]
  AND invoice_date < [this quarter start]

-- "Show me all unpaid bills."
SELECT * FROM invoices
WHERE status IN ('received','validated','approved','on_hold')
```

---

## Web UI Tabs

| Tab | Description |
|-----|-------------|
| **Ask a Question** | Chat interface — type NL questions, see SQL, results, charts, and clarification options |
| **Schema Explorer** | Browse all 12 tables with columns, types, and sample values (auto-discovered) |
| **Semantic Layer** | View metrics, synonyms, relationships, and table descriptions |
| **Query Cache** | Browse cached queries, hit counts, and re-run any cached question |

---

## Bonus Features Implemented

| Feature | Status | Details |
|---------|--------|---------|
| Semantic Layer | ✅ | `semantic_layer.yaml` — tables, columns, synonyms, metrics, relationships, temporal |
| NLP-to-SQL Engine | ✅ | Claude + semantic layer → validated SQL → executed result |
| Ambiguity Handling | ✅ | Two-layer: Python patterns (reliable) + LLM flags; clickable clarification buttons |
| Self-Correction Retry | ✅ | Bad SQL fed back to Claude with valid tables list; auto-corrects before showing error |
| Query Caching | ✅ | Trigram similarity matching; full result + ambiguity state stored per entry |
| Multi-Turn Conversations | ✅ | Session-based history (last 10 turns) passed to Claude as context |
| SQL Explanation | ✅ | Plain-English explanation returned with every query |
| Visualization Suggestions | ✅ | Auto-suggests bar / line / pie / table / scalar based on result shape |
| Schema Auto-Discovery | ✅ | `schema_discovery.py` reads live schema from in-memory SQLite at startup |
| In-Memory Database | ✅ | `db.py` loads `.sql` dump at startup — no `.db` file left on disk |
| Web UI | ✅ | Chat-style SPA with 4 tabs, Cashflo brand colors |
| CLI REPL | ✅ | Interactive REPL with cache support and conversation history |
