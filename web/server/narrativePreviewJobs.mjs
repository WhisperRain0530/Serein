import { randomUUID } from "node:crypto";

export function createNarrativePreviewJobs({ now = Date.now, ttlMs = 600_000, timeoutMs = 330_000 } = {}) {
  const jobs = new Map();

  function prune() {
    for (const [id, job] of jobs) {
      if (job.state === "done" && now() - job.completedAt > ttlMs) jobs.delete(id);
    }
  }

  return {
    start(work) {
      prune();
      if ([...jobs.values()].filter((job) => job.state === "pending").length >= 3) return null;
      const id = randomUUID();
      const job = { state: "pending", createdAt: now(), timeoutMs, result: null };
      jobs.set(id, job);
      const controller = new AbortController();
      const finish = (result) => {
        if (job.state !== "pending") return;
        clearTimeout(timer);
        job.state = "done";
        job.completedAt = now();
        job.result = result;
      };
      const fail = (error) => {
        if (job.state !== "pending") return;
        console.error("[serein-memory-bridge] Narrative preview failed", error);
        const writerTimeout = error?.message === "narrative_writer_timeout";
        const timedOut = writerTimeout || ["AbortError", "TimeoutError"].includes(error?.name);
        finish({
          statusCode: timedOut ? 504 : 502,
          payload: {
            status: "error",
            reason: "narrative_preview_failed",
            message: writerTimeout ? (job.timeoutMs===420_000?"叙事卷订阅任务等待超时，请重新预览。":"叙事卷生成超过 5 分钟，请重新预览。")
              : timedOut ? "预览请求超时，请重新预览。" : "暂时没有生成叙事卷预览。",
            writes_performed: [],
          },
        });
      };
      const expire = () => {
        const error = new Error("narrative_writer_timeout");
        fail(error);
        controller.abort(error);
      };
      let timer = setTimeout(expire, timeoutMs);
      timer.unref?.();
      const setJobTimeout = (milliseconds) => {
        if (job.state !== 'pending' || milliseconds !== 420_000) return;
        job.timeoutMs=milliseconds;
        clearTimeout(timer);
        timer=setTimeout(expire,Math.max(0,job.createdAt+milliseconds-now()));
        timer.unref?.();
      };
      void Promise.resolve().then(() => work(controller.signal,setJobTimeout)).then(
        finish, fail,
      );
      return id;
    },
    get(id) {
      prune();
      return jobs.get(id) || null;
    },
  };
}
