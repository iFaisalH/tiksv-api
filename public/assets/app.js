const form = document.getElementById("form");
const input = document.getElementById("input");
const submit = document.getElementById("submit");
const error = document.getElementById("error");

function formatSeconds(ms) {
  return `${(ms / 1000).toFixed(1)}s`;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.hidden = true;
  const label = submit.textContent;
  submit.disabled = true;
  const start = performance.now();
  const timer = setInterval(() => {
    submit.textContent = formatSeconds(performance.now() - start);
  }, 100);
  const stopTimer = () => {
    clearInterval(timer);
    return performance.now() - start;
  };
  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: input.value.trim() }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "download failed");
    }
    const name = res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] ?? "tiktok.mp4";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
    submit.textContent = formatSeconds(stopTimer());
    setTimeout(() => {
      submit.textContent = label;
    }, 3000);
  } catch (err) {
    stopTimer();
    submit.textContent = label;
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
