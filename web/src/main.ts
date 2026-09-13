import { mount } from "svelte";
import App from "./App.svelte";
import "./lib/theme.css";
import { installKeyboardInset } from "./lib/keyboard";

// Mobile first: keeps the composer above the on-screen keyboard. No-op where
// the visual viewport matches the window (desktop), so it is not gated.
installKeyboardInset();

const app = mount(App, {
  target: document.getElementById("app") as HTMLElement,
});

export default app;
