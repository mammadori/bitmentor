const toggle = document.querySelector(".menu-toggle");
if (toggle) {
  toggle.addEventListener("click", () => {
    const open = document.body.classList.toggle("menu-open");
    toggle.setAttribute("aria-expanded", String(open));
  });
}

for (const button of document.querySelectorAll("[data-seek]")) {
  button.addEventListener("click", () => {
    const video = document.querySelector("video");
    video.currentTime = Number(button.dataset.seek);
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    video.scrollIntoView({behavior: still ? "auto" : "smooth", block: "center"});
    video.play();
  });
}

for (const button of document.querySelectorAll("[data-print]")) {
  button.addEventListener("click", () => print());
}

const observer = "IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches
  ? new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
        observer.unobserve(entry.target);
      }
    }
  }, { rootMargin: "0px 0px -8% 0px" })
  : null;

for (const element of document.querySelectorAll("article > section:not(.hero, .stats), .reveal")) {
  if (observer) {
    element.classList.add("reveal-ready");
    observer.observe(element);
  }
}
