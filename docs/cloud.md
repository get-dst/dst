# dst Cloud

<div class="dst-cloud-hero">
  <p class="dst-cloud-status">$ dst cloud &nbsp;·&nbsp; status: <span>not built yet</span></p>
  <p class="dst-cloud-head">The open-source dst, run by the people who build it.</p>
  <p class="dst-cloud-sub">You author the lenses and definitions. We carry the deploy,
  the upgrades, the migrations, the backups, and the pager. Your warehouse stays yours;
  we hold configuration and traces — a trace carries the question, the SQL and the
  answer, and result rows only for a lens that opts in with
  <code>logging.log_samples</code>.</p>
</div>

<div class="dst-cloud-rows">
  <div class="dst-cloud-row">
    <span>SAME PRODUCT</span>
    <p>The Apache-2.0 dst you can read in the open repo, unmodified. Nothing held
    back for the hosted version; the guarantees in these docs are the product.</p>
  </div>
  <div class="dst-cloud-row">
    <span>NO LOCK-IN, BY CONSTRUCTION</span>
    <p>Lenses, definitions, and certified answers are files in your repo. Nothing to
    export that you do not already hold: point a self-hosted dst at the same files
    and leave any day.</p>
  </div>
  <div class="dst-cloud-row">
    <span>FOR TEAMS WHO WANT THE LAYER, NOT ANOTHER SERVICE TO RUN</span>
    <p>If you want the governed answers, the receipts, and the KPIs without owning
    one more deployment, this is the lane.</p>
  </div>
</div>

It does not exist yet. We are building it, and early access will be small. Leave
an address and you'll get one email, from a person, when there is something real
to try.

<form id="cloud-signup" class="dst-signup">
  <label>Work email
    <input type="email" name="email" required placeholder="you@example.com">
  </label>
  <label>Name
    <input type="text" name="name" placeholder="optional">
  </label>
  <label>Company
    <input type="text" name="company" placeholder="optional">
  </label>
  <label>What would you point it at?
    <textarea name="note" rows="3" placeholder="warehouse, stack, the questions your agents keep getting wrong (optional)"></textarea>
  </label>
  <div class="dst-signup-err" id="cloud-signup-err"></div>
  <button type="submit">Notify me about early access</button>
</form>

<script>
document.getElementById("cloud-signup").addEventListener("submit", function (e) {
  e.preventDefault();
  var form = e.target;
  var err = document.getElementById("cloud-signup-err");
  err.textContent = "";
  var data = {};
  ["email", "name", "company", "note"].forEach(function (k) { data[k] = form.elements[k].value; });
  form.querySelector("button").disabled = true;
  fetch("/waitlist/signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data)
  }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
    .then(function (r) {
      if (r.ok) {
        form.outerHTML = '<p class="dst-signup-done">You\'re on the list. One email, from a person, when early access opens.</p>';
      } else {
        err.textContent = r.body.error || "That didn't go through. Try again.";
        form.querySelector("button").disabled = false;
      }
    })
    .catch(function () {
      err.textContent = "That didn't go through. Try again.";
      form.querySelector("button").disabled = false;
    });
});
</script>
