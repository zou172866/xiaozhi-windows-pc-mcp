import { defineConfig } from "vite";

export default defineConfig({
  build: {
    license: { fileName: "dependency-notices.md" },
    rolldownOptions: {
      onwarn(warning, warn) {
        // This entire example runs in the browser; there are no React server components.
        if (
          warning.code === "MODULE_LEVEL_DIRECTIVE" &&
          warning.message.includes("use client")
        )
          return;
        warn(warning);
      },
    },
  },
});
