# Outreach Pipeline — Flow Chart

Source: [PLAN.md](PLAN.md). Happy path, plus the two placeholder branches for edge cases.

```mermaid
flowchart TD
    A([Rep enters prospect:<br/>name, company, role<br/>+ optional website, notes]) --> V{Required fields<br/>filled?}
    V -- No --> A
    V -- Yes --> R[Create run<br/>status = running]

    R --> S2[2 · Plan queries<br/>fill company + person templates<br/><i>code</i>]
    S2 --> S3[3 · Gather sources<br/>run queries in parallel, dedupe URLs,<br/>add pasted notes<br/><i>search tool</i>]
    S3 --> S4[4 · Extract signals<br/>type, level, summary, evidence, URL, date<br/><i>LLM</i>]
    S4 --> C4{Code checks:<br/>URL was gathered?<br/>person signal names both<br/>person + company?}
    C4 -- Fails --> D4[Drop signal<br/>record reason]
    C4 -- Passes --> M4[Merge same event<br/>from several outlets]

    M4 --> S5[5 · Score each signal<br/>R relevance 0–3 · LLM<br/>T recency · P person/company · code<br/>score = R/3 × 0.6T + 0.4P]
    S5 --> G5{Any signal with<br/>R ≥ 2?}
    G5 -- No --> NR([needs_research<br/>no draft · stub])
    G5 -- Yes --> P5[Pick top score<br/>keep 2 runner-ups]

    P5 --> S6[6 · Draft<br/>subject + hook, bridge, value, CTA<br/>60–120 words<br/><i>LLM</i>]
    S6 --> S7{7 · Checks pass?<br/>length, banned phrases,<br/>names, grounding, one ask}
    S7 -- Yes --> DR[status = drafted]
    S7 -- No, 1st time --> S6
    S7 -- No, 2nd time --> FL[status = flagged<br/>failures highlighted]

    DR --> S8[8 · Review queue<br/>draft + why-this-hook panel<br/>+ runner-ups]
    FL --> S8
    S8 --> ACT{Rep action}
    ACT -- Approve --> AP([approved · text copied<br/>nothing sent])
    ACT -- Edit --> ED[Save edited text] --> S8
    ACT -- Regenerate --> S6
    ACT -- Discard --> DI([discarded + reason])

    S3 -. error / timeout .-> F([failed])
    S4 -. error / timeout .-> F
    S6 -. error / timeout .-> F

    classDef llm fill:#e8defc,stroke:#7b5cd6;
    classDef code fill:#dff3e4,stroke:#3a9a5b;
    classDef human fill:#fdebd3,stroke:#d98a2b;
    classDef stop fill:#f6d5d5,stroke:#c0504d;
    class S4,S6 llm;
    class S2,C4,M4,S5,G5,P5,S7 code;
    class A,S8,ACT,ED human;
    class NR,F,DI stop;
```

Colours: purple = LLM, green = code, orange = rep, red = stop states.

## Run states

```mermaid
stateDiagram-v2
    [*] --> running
    running --> drafted
    running --> flagged: checks failed twice
    running --> needs_research: no signal passed gate
    running --> failed: error or timeout
    drafted --> approved
    drafted --> discarded
    flagged --> approved
    flagged --> discarded
```
