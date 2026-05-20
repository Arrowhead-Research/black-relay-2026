const CTA_LINKS = {
  // Replace these placeholders when official application/contact URLs are available.
  apply: "mailto:contact@example.com?subject=Black%20Relay%202026%20Participant%20Interest",
  advisor: "mailto:contact@example.com?subject=Black%20Relay%202026%20Advisor%20Interest",
  contact: "mailto:contact@example.com?subject=Black%20Relay%202026",
};

for (const anchor of document.querySelectorAll("[data-link]")) {
  const key = anchor.dataset.link;
  if (CTA_LINKS[key]) anchor.href = CTA_LINKS[key];
}

const navToggle = document.querySelector(".nav-toggle");
const siteNav = document.querySelector(".site-nav");

navToggle?.addEventListener("click", () => {
  const isOpen = siteNav.classList.toggle("is-open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
});

siteNav?.addEventListener("click", (event) => {
  if (event.target instanceof HTMLAnchorElement) {
    siteNav.classList.remove("is-open");
    navToggle?.setAttribute("aria-expanded", "false");
  }
});
