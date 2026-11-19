import graph from "../../../docs/assets/brain-flow.json";
import chinese from "../../../docs/assets/brain-flow.zh-CN.json";

export type AtlasLanguage = "en" | "zh-CN";
export type Translate = (text: string) => string;

export function atlasLanguage(search: string): AtlasLanguage {
  return new URLSearchParams(search).get("lang") === "zh-CN" ? "zh-CN" : "en";
}

export function atlasContent(language: AtlasLanguage) {
  const ui: Record<string, string> = chinese.ui;
  const t: Translate = (text) =>
    language === "zh-CN" ? (ui[text] ?? text) : text;
  // Only copy text fields. Geometry, edges, active stages and formulas remain shared.
  const flow =
    language === "en"
      ? graph
      : {
          ...graph,
          views: graph.views.map((view) => {
            const translated =
              chinese.views[view.id as keyof typeof chinese.views];
            const nodes: Record<
              string,
              { title: string; subtitle: string; tag: string }
            > = translated.nodes;
            return {
              ...view,
              label: translated.label,
              eyebrow: translated.eyebrow,
              description: translated.description,
              nodes: view.nodes.map((node) => ({ ...node, ...nodes[node.id] })),
              steps: view.steps.map((step, i) => ({
                ...step,
                ...translated.steps[i],
              })),
            };
          }),
        };
  return { t, flow };
}
