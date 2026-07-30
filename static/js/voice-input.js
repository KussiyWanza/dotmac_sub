(() => {
  "use strict";

  const MAX_RECORDING_MS = 120000;
  const MIME_TYPES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];

  const csrfToken = () =>
    document.querySelector('meta[name="csrf-token"]')?.content || "";

  const supportedMimeType = () =>
    MIME_TYPES.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) || "";

  const setStatus = (node, message) => {
    if (node) node.textContent = message;
  };

  function attach(textarea) {
    if (textarea.dataset.voiceAttached === "true") return;
    const scope =
      textarea.closest("[data-reply-composer]") ||
      textarea.closest("[data-voice-scope]") ||
      textarea.parentElement;
    const trigger = scope?.querySelector("[data-voice-trigger]");
    const status = scope?.querySelector("[data-voice-status]");
    if (!trigger || !status) return;

    textarea.dataset.voiceAttached = "true";
    let recorder = null;
    let stream = null;
    let chunks = [];
    let stopTimer = null;
    let starting = false;
    let pointerHeld = false;
    let recordingStartedAt = 0;

    const setRecording = (active) => {
      trigger.setAttribute("aria-pressed", active ? "true" : "false");
    };

    const releaseMedia = () => {
      window.clearTimeout(stopTimer);
      stopTimer = null;
      stream?.getTracks?.().forEach((track) => track.stop());
      stream = null;
      recorder = null;
      setRecording(false);
    };

    const insertTranscript = (text) => {
      const clean = String(text || "").trim();
      if (!clean) return;
      const existing = textarea.value.trimEnd();
      textarea.value = existing ? `${existing}\n${clean}` : clean;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      textarea.focus();
    };

    const upload = async (blob, durationMs) => {
      setStatus(status, "Transcribing recording…");
      const form = new FormData();
      form.append("audio", blob, "recording");
      form.append("context", textarea.dataset.voiceContext || "crm_reply");
      form.append("duration_ms", String(durationMs));
      try {
        const response = await fetch("/admin/inbox/voice/transcription", {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken() },
          body: form,
        });
        const payload = await response.json().catch(() => ({}));
        if (!payload.ok) {
          throw new Error(payload.error || "Transcription failed.");
        }
        insertTranscript(payload.text);
        setStatus(status, "Transcript inserted. Review it before sending.");
      } catch (error) {
        setStatus(status, error.message || "Transcription failed.");
      }
    };

    const stop = () => {
      pointerHeld = false;
      if (recorder?.state === "recording") recorder.stop();
    };

    const start = async (event) => {
      if (starting || recorder?.state === "recording") return;
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        setStatus(status, "Voice recording is not supported in this browser.");
        return;
      }
      event.preventDefault();
      pointerHeld = true;
      starting = true;
      chunks = [];
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mimeType = supportedMimeType();
        recorder = mimeType
          ? new MediaRecorder(stream, { mimeType })
          : new MediaRecorder(stream);
        recorder.addEventListener("dataavailable", (item) => {
          if (item.data?.size) chunks.push(item.data);
        });
        recorder.addEventListener(
          "stop",
          async () => {
            const recordedType =
              recorder?.mimeType || mimeType || chunks[0]?.type || "audio/webm";
            const blob = new Blob(chunks, { type: recordedType });
            const durationMs = Math.max(1, Date.now() - recordingStartedAt);
            chunks = [];
            releaseMedia();
            if (!blob.size) {
              setStatus(status, "No audio was recorded.");
              return;
            }
            await upload(blob, durationMs);
          },
          { once: true },
        );
        recorder.addEventListener(
          "error",
          () => {
            chunks = [];
            releaseMedia();
            setStatus(status, "Recording failed. Please try again.");
          },
          { once: true },
        );
        recorder.start();
        recordingStartedAt = Date.now();
        setRecording(true);
        trigger.setPointerCapture?.(event.pointerId);
        setStatus(status, "Recording. Release to stop.");
        stopTimer = window.setTimeout(stop, MAX_RECORDING_MS);
        if (!pointerHeld) stop();
      } catch (_error) {
        pointerHeld = false;
        releaseMedia();
        setStatus(status, "Microphone permission is required to record.");
      } finally {
        starting = false;
      }
    };

    trigger.addEventListener("pointerdown", start);
    trigger.addEventListener("pointerup", stop);
    trigger.addEventListener("pointercancel", stop);
    trigger.addEventListener("lostpointercapture", stop);
    window.addEventListener("pagehide", releaseMedia);
  }

  const attachAll = (root = document) => {
    root
      .querySelectorAll?.("textarea[data-voice-enabled]")
      .forEach((textarea) => attach(textarea));
  };

  document.addEventListener("DOMContentLoaded", () => attachAll());
  document.addEventListener("htmx:afterSwap", (event) => attachAll(event.target));
})();
