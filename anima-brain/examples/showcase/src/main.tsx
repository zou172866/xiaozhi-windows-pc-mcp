import React from "react";
import { createRoot } from "react-dom/client";
import { MotionConfig } from "motion/react";
import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/noto-serif-jp/latin-400.css";
import App from "./App";
import Architecture from "./Architecture";
import "./style.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      {new URLSearchParams(location.search).get("view") === "architecture" ? (
        <Architecture />
      ) : (
        <App />
      )}
    </MotionConfig>
  </React.StrictMode>,
);
