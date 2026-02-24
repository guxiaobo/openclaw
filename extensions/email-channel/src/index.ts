/**
 * Email Channel Plugin for OpenClaw
 * Official Plugin SDK - No modifications required
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";
import { emailPlugin } from "./src/channel.js";
import { setEmailRuntime } from "./src/channel.js";

const plugin = {
  id: "email",
  name: "Email",
  description: "Email channel plugin for sending and receiving messages via IMAP/SMTP",
  configSchema: emptyPluginConfigSchema(),
  register(api: OpenClawPluginApi) {
    setEmailRuntime(api.runtime);
    api.registerChannel({ plugin: emailPlugin });
  },
};

export default plugin;
