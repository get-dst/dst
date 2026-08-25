/* dst Cloud CTA in the header. The sidebar nav entry alone was invisible. */
(function () {
  function inject() {
    var inner = document.querySelector(".md-header__inner");
    if (!inner || inner.querySelector(".dst-cloud-cta")) return;
    var a = document.createElement("a");
    a.className = "dst-cloud-cta";
    a.href = "/cloud/";
    a.textContent = "dst cloud";
    inner.appendChild(a);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", inject);
  } else {
    inject();
  }
})();
