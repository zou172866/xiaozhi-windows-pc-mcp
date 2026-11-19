import { applyHostStyleVariables } from '@modelcontextprotocol/ext-apps';
export function applyHostTheme(context = {}) {
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.theme === 'light' || context.theme === 'dark') document.documentElement.dataset.theme = context.theme;
}
