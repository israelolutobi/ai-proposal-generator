/*
 * ProposalQ cinematic demo controller.
 * Fictional examples only; no external requests.
 */
(function () {
    "use strict";

    const durations = [15000, 15000, 25000, 12000, 10000];
    let active = false;
    let paused = false;
    let stage = 0;
    let started = 0;
    let elapsed = 0;
    let frame = null;
    let completed = false;

    function content() {
        return document.getElementById("demo-content");
    }

    function stopFrame() {
        if (frame !== null) cancelAnimationFrame(frame);
        frame = null;
    }

    function updateButton() {
        const button = document.getElementById("demo-play");
        if (button) {
            button.textContent = active && !paused
                ? "● Auto Demo ON · Turn Off"
                : paused ? "▶ Resume Demo" : "○ Auto Demo OFF · Turn On";
        }
    }

    function showStage() {
        document.dispatchEvent(new CustomEvent("proposalq:demo-stage", {
            detail: { stage }
        }));
    }

    function animate(now) {
        if (!active || paused) return;

        const delta = Math.min(now - started, 100);
        elapsed += Math.max(0, delta);
        started = now;

        const element = content();
        const progress = Math.min(elapsed / durations[stage], 1);

        if (element) {
            if (stage === 0) {
                const max = Math.max(0, element.scrollHeight - element.clientHeight);
                element.scrollTop = max * Math.min(progress * 1.2, 1);
            }

            if (stage === 1 || stage === 3) {
                const items = element.querySelectorAll("li");
                items.forEach((item, index) => {
                    const revealAt = (index + 1) / (items.length + 1);
                    item.style.opacity = progress >= revealAt ? "1" : "0.15";
                    item.style.transform = progress >= revealAt
                        ? "translateX(0)" : "translateX(-8px)";
                    item.style.transition = "opacity .3s, transform .3s";
                });
            }

            if (stage === 2) {
                const paragraphs = element.querySelectorAll("p:not(.demo-note)");
                const total = Array.from(paragraphs).reduce(
                    (sum, p) => sum + (p.dataset.fullText || "").length, 0
                );
                let remaining = Math.floor(total * progress);

                paragraphs.forEach(p => {
                    const full = p.dataset.fullText || "";
                    const count = Math.min(remaining, full.length);
                    p.textContent = full.slice(0, count);
                    remaining -= count;
                });

                const visible = Array.from(paragraphs).filter(p => p.textContent);
                if (visible.length) {
                    const last = visible[visible.length - 1];
                    element.scrollTop = Math.max(
                        0, last.offsetTop - element.offsetTop - element.clientHeight / 2
                    );
                }
            }
        }

        if (progress >= 1) {
            if (stage === durations.length - 1) {
                stage = 0;
                elapsed = 0;
                showStage();
                prepareStage();
                started = now;
                frame = requestAnimationFrame(animate);
                return;
            }

            stage++;
            elapsed = 0;
            showStage();
            prepareStage();
        }

        frame = requestAnimationFrame(animate);
    }

    function prepareStage() {
        const element = content();
        if (!element) return;

        if (stage === 2) {
            element.querySelectorAll("p:not(.demo-note)").forEach(p => {
                p.dataset.fullText = p.textContent;
                p.textContent = "";
            });
        }

        if (stage === 4 && !element.querySelector(".demo-confetti")) {
            const overlay = document.createElement("div");
            overlay.className = "demo-confetti";
            overlay.setAttribute("aria-hidden", "true");

            const colors = ["#60a5fa", "#22d3ee", "#86efac", "#facc15"];
            for (let i = 0; i < 40; i++) {
                const piece = document.createElement("span");
                piece.style.left = Math.random() * 100 + "%";
                piece.style.background = colors[i % colors.length];
                piece.style.animationDelay = Math.random() * 1.2 + "s";
                overlay.appendChild(piece);
            }
            element.appendChild(overlay);
        }
    }

    function play() {
        if (active && !paused) {
            pause();
            return;
        }

        if (active && paused) {
            paused = false;
        } else {
            stopFrame();
            stage = 0;
            elapsed = 0;
            active = true;
            paused = false;
            completed = false;
            showStage();
            prepareStage();
        }

        started = performance.now();
        updateButton();
        frame = requestAnimationFrame(animate);
    }

    function pause() {
        paused = true;
        stopFrame();
        updateButton();
    }

    function stop() {
        active = false;
        paused = false;
        stopFrame();
        updateButton();
    }

    function goToStage(index) {
        if (!active || paused) return;

        stopFrame();
        stage = Math.max(0, Math.min(index, durations.length - 1));
        elapsed = 0;
        prepareStage();
        started = performance.now();
        frame = requestAnimationFrame(animate);
    }

    function getState() {
        return { playing: active && !paused, paused, stage, completed };
    }

    window.ProposalQDemoAnimation = { play, pause, stop, goToStage, getState };
})();
