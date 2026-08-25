---
hide:
  - navigation
  - toc
---

# The product, on screen

One product, two seats. The person asking sees answers inside the AI they
already use — they never see dst at all. The engineer sees the files those
answers stand on. Both views below are the same live demo project: **the
definition the chat answer cites is the highlighted line in the editor
underneath it.** Change that line, and the answer changes.

## Seat one — the person asking

No dashboard, no new tool to learn. Their AI calls dst behind the scenes, and
the answer comes back with the SQL that produced it, a confidence grade, and
the cost:

<div class="dst-onscreen">
<style>
  .dst-onscreen .chatwin {
    background: #14100d; border: 1px solid #3a332c; border-radius: 8px;
    overflow: hidden; font-size: 13.5px; color: #d9d2c5; max-width: 760px;
    font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    line-height: 1.55; margin: 0 auto;
  }
  .dst-onscreen .chatwin .titlebar {
    text-align: center; padding: 7px; font-size: 11px; color: #857b6f;
    background: #1d1815; border-bottom: 1px solid #3a332c;
  }
  .dst-onscreen .chat { padding: 20px 22px 24px; display: flex; flex-direction: column; gap: 14px; }
  .dst-onscreen .msg-u {
    align-self: flex-end; max-width: 85%;
    background: #2a211a; border: 1px solid #3a332c; border-radius: 10px 10px 2px 10px;
    padding: 9px 14px; color: #efe9dd;
  }
  .dst-onscreen .toolline {
    font-size: 11px; color: #857b6f;
    border-left: 2px solid #d78700; padding-left: 10px;
  }
  .dst-onscreen .msg-a { max-width: 92%; }
  .dst-onscreen .rcpt {
    margin-top: 8px; border: 1px solid #3a332c; background: #191411;
    border-radius: 6px; padding: 10px 14px; font-size: 11.5px;
  }
  .dst-onscreen .rcpt div { display: flex; gap: 12px; }
  .dst-onscreen .rcpt .rk { color: #d78700; min-width: 88px; }
  .dst-onscreen .rcpt .rv { color: #b3a894; overflow-x: auto; white-space: nowrap; }
  .dst-onscreen .rcpt .ok { color: #7cbd82; font-weight: 700; }
  .dst-onscreen .opts { margin: 6px 0 0 0; padding: 0 0 0 18px; color: #b3a894; }
</style>

<div class="chatwin">
  <div class="titlebar">the AI your team uses — any of them</div>
  <div class="chat">
    <div class="msg-u">Where should we look for growth?</div>
    <div class="toolline">asked dst — how many customers are repeat customers?</div>
    <div class="toolline">asked dst — what was total revenue?</div>
    <div class="toolline">asked dst — revenue by month… <span style="opacity:.7">(and more)</span></div>
    <div class="msg-a">Your clearest lever is repeat purchases. Of your 100
    customers, only 19 have bought more than once — 81 bought exactly once
    and never came back. Total revenue stands at €1,739, so converting even
    a fraction of those one-time buyers into second orders is the fastest
    growth path in this data.</div>
    <div class="msg-u">And what is the average value of a customer?</div>
    <div class="toolline">asked dst — clarify came back</div>
    <div class="msg-a">Before I answer — 'value' means two different things in
    your data. Which do you want?
      <ul class="opts">
        <li>lifetime value — total historical revenue per customer</li>
        <li>order amount — a single order's total</li>
      </ul>
    </div>
  </div>
</div>

</div>

The person sees conclusions, not queries. Behind that first answer their AI
made several dst calls, and **each one** was checked against the lens's
definitions and logged with its SQL, its confidence grade, and its cost —
that record exists for the engineer and the auditor, not on this screen.

Two things to notice. "Bought more than once" is not the model's guess —
it is your governed definition of a repeat customer, and you will see the
exact file it lives in below. And the second answer is a question back:
'value' means two different things in this data, so dst
[asks rather than guesses](concepts/clarify-and-refusal.md).

<p style="text-align:center; font-family:ui-monospace,Menlo,monospace; font-size:0.8rem; color:#b45309; margin:1.6rem 0;">
▲ the answer &nbsp;·&nbsp; the file it stands on ▼
</p>

## Seat two — the engineer

Everything the chat answer stood on is a file in a folder, in version
control. The definition the answer cited — `repeat_customer` — is open below,
its wording highlighted. The lens (who may ask, over what data) and the
certified answers (question-and-SQL pairs a person approved) are the other
two tabs. Underneath, the terminal: `dst plan` previews the change, `dst
apply` publishes it, `dst test` re-checks every certified answer against it.
All of it real: the files verbatim (`lens.yaml` abridged), the terminal from
a live capture. The tabs and the highlighted tree files are clickable.

<div class="dst-onscreen">
<style>
  /* this page runs full-bleed: both sidebars are hidden via front-matter,
     and the grid widens so the editor window gets real room */
  body:has(.dst-onscreen) .md-grid { max-width: 96rem; }
  .dst-onscreen { margin-top: 0.6rem; }
  .dst-onscreen .win {
    background: #14100d; border: 1px solid #3a332c; border-radius: 8px;
    overflow: hidden; font-size: 13px; color: #d9d2c5;
    font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    line-height: 1.5;
  }
  .dst-onscreen .titlebar {
    text-align: center; padding: 7px; font-size: 11px; color: #857b6f;
    background: #1d1815; border-bottom: 1px solid #3a332c;
  }
  .dst-onscreen .main { display: grid; grid-template-columns: 250px 1fr; }
  .dst-onscreen .tree {
    background: #191411; border-right: 1px solid #3a332c;
    padding: 10px 0 14px; font-size: 12px; line-height: 1.7; overflow-x: auto;
  }
  .dst-onscreen .tree .lab { font-size: 9.5px; letter-spacing: .12em; color: #857b6f; padding: 0 14px 6px; }
  .dst-onscreen .tree div { padding: 0 14px; white-space: pre; color: #b3a894; }
  .dst-onscreen .tree .dir { color: #d9d2c5; }
  .dst-onscreen .tree .openable { cursor: pointer; }
  .dst-onscreen .tree .openable:hover { background: #221b15; }
  .dst-onscreen .tree .active { background: #2a211a; color: #efe9dd; }
  .dst-onscreen .tree .active b { color: #d78700; font-weight: 700; }
  .dst-onscreen .editor { display: flex; flex-direction: column; min-width: 0; }
  .dst-onscreen .tabs { display: flex; background: #191411; border-bottom: 1px solid #3a332c; font-size: 11.5px; }
  .dst-onscreen .tabs button {
    padding: 8px 16px; color: #857b6f; border: 0; border-right: 1px solid #3a332c;
    background: transparent; font: inherit; cursor: pointer;
  }
  .dst-onscreen .tabs button:hover { color: #d9d2c5; }
  .dst-onscreen .tabs button:focus-visible { outline: 1px solid #d78700; outline-offset: -1px; }
  .dst-onscreen .tabs button.on { background: #14100d; color: #efe9dd; border-top: 2px solid #d78700; padding-top: 6px; }
  .dst-onscreen .pane { display: none; }
  .dst-onscreen .pane.on { display: flex; }
  .dst-onscreen .code { padding: 12px 0; overflow-x: auto; flex: 1; }
  .dst-onscreen .gutter {
    text-align: right; padding: 0 12px 0 16px; color: #4d443b; user-select: none;
    white-space: pre; font-size: 12.5px; line-height: 1.62;
  }
  .dst-onscreen .srcpane {
    white-space: pre; font-size: 12.5px; line-height: 1.62; padding-right: 20px;
    font-family: inherit;
  }
  .dst-onscreen .k { color: #d78700; }
  .dst-onscreen .s { color: #d9d2c5; }
  .dst-onscreen .li { color: #857b6f; }
  .dst-onscreen .hl { background: #2a211a; display: inline-block; width: 100%; }
  .dst-onscreen .termpanel { border-top: 1px solid #3a332c; background: #120e0b; }
  .dst-onscreen .plab {
    font-size: 9.5px; letter-spacing: .12em; color: #857b6f;
    padding: 7px 16px 0; text-transform: uppercase;
  }
  .dst-onscreen .termpanel pre {
    margin: 0; padding: 8px 16px 16px; font-family: inherit; background: transparent;
    font-size: 12.5px; line-height: 1.6; overflow-x: auto; color: #d9d2c5; border: 0;
  }
  .dst-onscreen .b { font-weight: 700; color: #efe9dd; }
  .dst-onscreen .g { color: #7cbd82; font-weight: 700; }
  .dst-onscreen .y { color: #d9b45b; }
  .dst-onscreen .a { color: #d78700; }
  .dst-onscreen .d { color: #857b6f; }
</style>

<div class="win">
  <div class="titlebar" id="dst-titlebar">demo — semantic/definitions/examples/repeat-customer.md</div>
  <div class="main">
    <div class="tree">
      <div class="lab">EXPLORER</div>
      <div class="dir">demo/</div>
      <div>  AGENTS.md</div>
      <div>  README.md</div>
      <div>  docker-compose.yml</div>
      <div>  dst.yaml</div>
      <div class="dir">  fixtures/</div>
      <div>    jaffle_shop.duckdb</div>
      <div class="dir">  profiles/</div>
      <div>    jaffle.json</div>
      <div>    jaffle.probe.json</div>
      <div class="dir">  semantic/</div>
      <div class="dir">    entities/examples/</div>
      <div>      customers.yaml</div>
      <div>      orders.yaml</div>
      <div class="dir">    definitions/examples/</div>
      <div>      lifetime-value.md</div>
      <div class="openable" data-open="def">      <b>repeat-customer.md</b></div>
      <div>      value.md</div>
      <div class="dir">  lenses/customer_value/</div>
      <div class="openable" data-open="lens">    lens.yaml</div>
      <div>    queries.yaml</div>
      <div class="openable" data-open="cert">    certified_answers.yaml</div>
      <div class="dir">    definitions/</div>
      <div>      revenue.md</div>
      <div class="dir">    evals/</div>
      <div>      cases.yaml</div>
      <div>    compiled.yaml</div>
    </div>
    <div class="editor">
      <div class="tabs" role="tablist">
        <button data-open="def" class="on">repeat-customer.md</button>
        <button data-open="lens">lens.yaml</button>
        <button data-open="cert">certified_answers.yaml</button>
      </div>
      <div class="pane code on" id="dst-pane-def" data-title="demo — semantic/definitions/examples/repeat-customer.md">
        <div class="gutter">1
2
3
4
5
6</div>
        <div class="srcpane"><span class="li">---</span>
<span class="k">metric</span>: <span class="s">repeat_customer</span>
<span class="k">sql</span>: <span class="s">customers.number_of_orders &gt; 1</span>
<span class="li">---</span>

<span class="hl"><span class="s">A repeat customer has more than one completed order (number_of_orders &gt; 1).</span></span></div>
      </div>
      <div class="pane code" id="dst-pane-lens" data-title="demo — lenses/customer_value/lens.yaml">
        <div class="gutter"> 1
 2
 3
 4
 5
 6
 7
 8
 9
10
11
12
13
14
15
16
17
18
19
20
21</div>
        <div class="srcpane"><span class="k">name</span>: <span class="s">customer_value</span>
<span class="k">display_name</span>: <span class="s">Customer Value</span>
<span class="k">description</span>: <span class="s">Customer lifetime value and order activity over the jaffle dataset.</span>
<span class="k">connections</span>:
<span class="li">-</span> <span class="s">jaffle</span>
<span class="k">select</span>:
  <span class="k">entities</span>:
  <span class="li">-</span> <span class="k">name</span>: <span class="s">customers</span>
  <span class="li">-</span> <span class="k">name</span>: <span class="s">orders</span>
  <span class="k">definitions</span>:
  <span class="li">-</span> <span class="s">lifetime_value</span>
  <span class="li">-</span> <span class="s">repeat_customer</span>
  <span class="li">-</span> <span class="s">value</span>
<span class="k">model</span>:
  <span class="k">temperature</span>: <span class="s">0.0</span>
  <span class="k">answer_mode</span>: <span class="s">balanced</span>
<span class="k">instructions</span>: <span class="s">Select explicit columns. Prefer month buckets for time series.</span>
<span class="k">access</span>:
  <span class="k">allow</span>: <span class="s">[]</span>
<span class="k">eval_gate</span>: <span class="s">block</span>
<span class="k">auto_review</span>: <span class="s">'off'</span></div>
      </div>
      <div class="pane code" id="dst-pane-cert" data-title="demo — lenses/customer_value/certified_answers.yaml">
        <div class="gutter">1
2
3
4
5
6
7
8</div>
        <div class="srcpane"><span class="li">-</span> <span class="k">question</span>: <span class="s">How many customers are repeat customers?</span>
  <span class="k">sql</span>: <span class="s">SELECT COUNT(customers.customer_id) AS repeat_customers FROM customers WHERE customers.number_of_orders &gt; 1</span>
  <span class="k">source</span>: <span class="s">"dst query req_7ba12c2be21b4d6c — verified against jaffle"</span>
  <span class="k">verified_by</span>: <span class="s">alex</span>
<span class="li">-</span> <span class="k">question</span>: <span class="s">What was total revenue?</span>
  <span class="k">sql</span>: <span class="s">SELECT SUM(orders.amount) AS revenue FROM orders WHERE orders.status != 'returned'</span>
  <span class="k">source</span>: <span class="s">"re-certified after ruling rev_81907144f8 — revenue excludes returned orders"</span>
  <span class="k">verified_by</span>: <span class="s">alex</span></div>
      </div>
      <div class="termpanel">
        <div class="plab">Terminal</div>
        <pre><span class="d">$</span> <span class="b">dst plan</span>
  <span class="y">~</span> semantic/definitions/examples/repeat-customer.md
  <span class="y">~</span> customer_value — STALE compile (shared changed: definition/repeat_customer)
  <span class="y">!</span> customer_value: 1 certified answer(s) need re-verify <span class="d">(--full lists them)</span>

<span class="b">Plan: 2 to change, 4 unchanged.</span> <span class="d">(--full shows diffs and hints)</span>
<span class="d">$</span> <span class="b">dst apply</span>
<span class="a">connections</span>: ok
<span class="a">semantic</span>: updated definition/repeat_customer
<span class="a">lens customer_value</span>: updated
<span class="g">Apply complete.</span> 4 warning(s), 0 error(s). <span class="d">(--json for the row array)</span>
<span class="d">$</span> <span class="b">dst test customer_value</span>
<span class="g">PASS</span>  me/customer_value: What was total revenue?                              <span class="d">9.7s</span>
<span class="g">PASS</span>  me/customer_value: How many customers are repeat customers?             <span class="d">37.2s</span>
<span class="g">PASS</span>  me/customer_value: What is the average value of a customer? <span class="d">[expect: clarify]</span>
<span class="a">────────────────────────────────────────────────────────────────</span>
<span class="b">3/3 passed</span> (2 certified + 1 behavioral) in org me</pre>
      </div>
    </div>
  </div>
</div>

<script>
  (function () {
    const root = document.currentScript.closest(".dst-onscreen");
    const tabs = root.querySelectorAll(".tabs button");
    const treeItems = root.querySelectorAll(".tree .openable");
    const titlebar = root.querySelector("#dst-titlebar");
    function open(id) {
      root.querySelectorAll(".pane").forEach(p => p.classList.remove("on"));
      const pane = root.querySelector("#dst-pane-" + id);
      pane.classList.add("on");
      titlebar.textContent = pane.dataset.title;
      tabs.forEach(b => b.classList.toggle("on", b.dataset.open === id));
      treeItems.forEach(el => {
        const active = el.dataset.open === id;
        el.classList.toggle("active", active);
        const name = el.textContent;
        el.innerHTML = active ? name.replace(/(\S.*)$/, "<b>$1</b>") : name;
      });
    }
    tabs.forEach(b => b.addEventListener("click", () => open(b.dataset.open)));
    treeItems.forEach(el => el.addEventListener("click", () => open(el.dataset.open)));
  })();
</script>
</div>

A definition's wording changed (the highlighted line). `plan` names everything
the change touches — the stale lens, the certified answer to re-verify — `apply`
publishes it, and `test` re-runs every certified answer against live generation:
**3/3 passed**. The revenue entry's `source` line carries the
[correction loop's](guides/correction-loop.md) trace: it was re-certified after a
review ruling changed what revenue means.
