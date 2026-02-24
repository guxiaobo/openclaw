/**
 * Email Channel Plugin for OpenClaw
 * Uses official Plugin SDK - no SDK modifications required
 */

import type {
  ChannelPlugin,
  OpenClawConfig,
  ChannelGatewayAdapter,
  ChannelSecurityContext,
  ChannelSecurityDmPolicy,
} from "openclaw/plugin-sdk";
import { startEmail, stopEmail, sendEmail, type EmailAttachment } from "./runtime.js";

// Email account configuration
interface EmailAccount {
  accountId?: string;
  enabled?: boolean;
  imap: {
    host: string;
    port: number;
    secure: boolean;
    user: string;
    password: string;
  };
  smtp: {
    host: string;
    port: number;
    secure: boolean;
    user: string;
    password: string;
  };
  checkInterval?: number;
  allowedSenders?: string[];
  maxAttachmentSize?: number;
}

// Store email context for outbound messaging
const emailContexts = new Map<string, { fromEmail: string; subject: string; messageId: string }>();

export function getEmailContext(sessionKey: string) {
  return emailContexts.get(sessionKey);
}

// Gateway adapter for email channel
const emailGatewayAdapter: ChannelGatewayAdapter<EmailAccount> = {
  startAccount: async (ctx) => {
    const account = ctx.account as EmailAccount;

    if (!account || !account.enabled) {
      ctx.log?.info?.(`[${account.accountId}] Email account disabled`);
      return;
    }

    if (
      !account.imap?.host ||
      !account.imap?.port ||
      !account.imap?.user ||
      !account.imap?.password
    ) {
      ctx.log?.error?.(`[${account.accountId}] Email IMAP configuration incomplete`);
      return;
    }

    if (
      !account.smtp?.host ||
      !account.smtp?.port ||
      !account.smtp?.user ||
      !account.smtp?.password
    ) {
      ctx.log?.error?.(`[${account.accountId}] Email SMTP configuration incomplete`);
      return;
    }

    ctx.log?.info?.(`[${account.accountId}] Starting email channel`);

    // Log allowed senders configuration with security warning
    if (account.allowedSenders && account.allowedSenders.length > 0) {
      ctx.log?.info?.(
        `[${account.accountId}] Only accepting emails from: ${account.allowedSenders.join(", ")}`,
      );
      ctx.log?.warn?.(
        `[${account.accountId}] WARNING: allowedSenders checks "From" address which can be forged. Use with IMAP server-level DKIM/SPF/DMARC validation for security.`,
      );
    } else {
      ctx.log?.info?.(`[${account.accountId}] Accepting emails from all senders`);
    }

    // Log attachment size limit
    const maxAttachmentSize = account.maxAttachmentSize || 10 * 1024 * 1024;
    ctx.log?.info?.(
      `[${account.accountId}] Maximum attachment size: ${(maxAttachmentSize / 1024 / 1024).toFixed(2)}MB`,
    );

    // Start email processing
    startEmail(
      account.accountId || "default",
      account,
      async (from, fromEmail, subject, body, messageId, uid, attachments) => {
        // Process attachments: save to temporary files
        const attachmentPaths: string[] = [];
        const attachmentsDir = `/tmp/openclaw-email-attachments/${Date.now()}`;

        if (attachments.length > 0) {
          try {
            const fs = await import("fs");
            const path = await import("path");

            if (!fs.existsSync(attachmentsDir)) {
              fs.mkdirSync(attachmentsDir, { recursive: true });
            }

            for (const att of attachments) {
              const sanitizedFilename = att.filename.replace(/[^a-zA-Z0-9.-]/g, "_");
              const filePath = path.join(attachmentsDir, sanitizedFilename);
              fs.writeFileSync(filePath, att.content);
              attachmentPaths.push(filePath);

              ctx.log?.info?.(
                `[${account.accountId}] Saved attachment: ${att.filename} to ${filePath}`,
              );
            }
          } catch (error) {
            ctx.log?.error?.(`[${account.accountId}] Error saving attachments: ${String(error)}`);
          }
        }

        // Build formatted message envelope
        let message = `From: ${from}\nSubject: ${subject}\n\n${body}`;

        // Add attachment information
        if (attachments.length > 0) {
          message += `\n\n--- Attachments ---\n`;
          for (let i = 0; i < attachments.length; i++) {
            const att = attachments[i];
            message += `- ${att.filename} (${(att.size / 1024).toFixed(2)}KB, ${att.contentType})\n`;
            if (attachmentPaths[i]) {
              message += `  File: ${attachmentPaths[i]}\n`;
            }
          }
        }

        // Add system instruction for file generation
        message += `\n\n--- System Instructions ---\n`;
        message += `This is an email channel. If you need to generate any files (images, documents, code, etc.):\n\n`;
        message += `STEPS TO FOLLOW:\n`;
        message += `1. IMPORTANT: Save files to ONE of these allowed directories (choose only one):\n`;
        message += `   - /tmp/ (recommended: /tmp/filename.ext)\n`;
        message += `   - /tmp/openclaw-generated/ (e.g., /tmp/openclaw-generated/filename.ext)\n`;
        message += `   - ~/.openclaw/workspace/ (e.g., /Users/username/.openclaw/workspace/filename.ext)\n\n`;
        message += `2. DO NOT copy the same file to multiple locations. Each file should only exist in ONE path.\n\n`;
        message += `3. After saving, mention the file path in your response. The system will:\n`;
        message += `   - Extract the file path automatically\n`;
        message += `   - Attach it to the email reply\n\n`;
        message += `4. The system will deduplicate files by filename, so avoid naming conflicts.\n\n`;
        message += `EXAMPLES:\n`;
        message += `✅ CORRECT: Save to /tmp/SystemInfo.java (single location)\n`;
        message += `❌ AVOID: Copying to both /tmp/SystemInfo.java AND /workspace/SystemInfo.java\n\n`;
        message += `NOTE: Only files in the allowed directories will be attached. `;
        message += `Duplicate files (same filename) will be deduplicated automatically.`;

        // Use fromEmail as sessionKey so all emails from the same sender are in one conversation
        const sessionKey = `email:${fromEmail}`;

        // Create a readable title for the session in Dashboard
        const title = `📧 ${fromEmail}${subject ? ` - ${subject}` : ""}`;

        ctx.log?.info?.(
          `[${account.accountId}] Processing email from ${fromEmail}: "${subject}" (UID: ${uid}, Attachments: ${attachments.length})`,
        );

        try {
          // Store email context for outbound messaging
          emailContexts.set(sessionKey, { fromEmail, subject, messageId });

          // Check if channelRuntime is available (Plugin SDK 2026.2.19+)
          // Gracefully handle older SDK versions with a warning instead of throwing
          if (!ctx.channelRuntime) {
            ctx.log?.warn?.(
              `[${account.accountId}] channelRuntime not available - requires Plugin SDK 2026.2.19+. Skipping AI response.`,
            );
            return;
          }

          // Use channelRuntime to dispatch the message
          const core = ctx.channelRuntime;

          // Use the dispatch function to process the message
          const result = await core.reply.dispatchReplyWithBufferedBlockDispatcher({
            ctx: {
              Body: message,
              RawBody: body,
              CommandBody: body,
              From: `email:${fromEmail}`,
              To: `${account.accountId || "default"}:${fromEmail}|${subject}|${messageId}`, // Format: "accountId:email|subject|messageId"
              SessionKey: sessionKey,
              AccountId: account.accountId,
              ChatType: "direct" as const,
              ConversationLabel: from,
              SenderName: from,
              SenderId: fromEmail,
              Provider: "email",
              Surface: "email",
              MessageSid: messageId,
              Timestamp: Date.now(),
            },
            cfg: ctx.cfg,
            dispatcherOptions: {
              responsePrefix: undefined,
              humanDelay: core.reply.resolveHumanDelayConfig(ctx.cfg, "default"),
              deliver: async (payload: any, info: any) => {
                // Send the reply via email
                const replyText = payload.text || "";

                // Extract files from payload (agent-generated media)
                const attachments: Array<{
                  path: string;
                  filename?: string;
                  contentType?: string;
                }> = [];

                // Handle single media file from payload
                if (payload.mediaUrl) {
                  attachments.push({
                    path: payload.mediaUrl,
                    contentType: payload.channelData?.MediaType as string | undefined,
                  });
                }

                // Handle multiple media files from payload
                if (payload.mediaUrls && payload.mediaUrls.length > 0) {
                  const mediaTypes =
                    (payload.channelData?.MediaTypes as string[] | undefined) || [];
                  for (let i = 0; i < payload.mediaUrls.length; i++) {
                    attachments.push({
                      path: payload.mediaUrls[i],
                      contentType: mediaTypes[i],
                    });
                  }
                }

                // Fallback: Extract file paths from text using strict patterns
                // Only match paths in specific allowed directories to avoid false positives
                const strictPathPatterns = [
                  // Match /tmp/ paths (common temporary directory)
                  /\/tmp\/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+/g,
                  // Match openclaw workspace paths
                  /\/Users\/[a-zA-Z0-9_\-./]+\/\.openclaw\/workspace\/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+/g,
                ];

                // Collect all paths first, then deduplicate
                const extractedPaths: string[] = [];

                for (const pattern of strictPathPatterns) {
                  const matches = replyText.match(pattern);
                  if (matches) {
                    for (const match of matches) {
                      // Clean up the path
                      const cleanPath = match.trim();

                      // Additional validation: must have a file extension
                      if (!cleanPath.match(/\.[a-zA-Z0-9]+$/)) {
                        continue;
                      }

                      // Avoid duplicates in extraction phase
                      if (!extractedPaths.includes(cleanPath)) {
                        extractedPaths.push(cleanPath);
                      }
                    }
                  }
                }

                // Deduplicate by filename - if multiple paths have the same filename, keep only one
                const filenameMap = new Map<string, string>();
                for (const path of extractedPaths) {
                  const filename = path.split("/").pop() || path;
                  // Keep the first occurrence (prefer /tmp/ over workspace)
                  if (!filenameMap.has(filename)) {
                    filenameMap.set(filename, path);
                  } else {
                    // If current path is /tmp/, prefer it over workspace
                    const existingPath = filenameMap.get(filename)!;
                    if (path.startsWith("/tmp/") && !existingPath.startsWith("/tmp/")) {
                      filenameMap.set(filename, path);
                    }
                  }
                }

                // Add deduplicated paths to attachments
                for (const path of filenameMap.values()) {
                  // Avoid duplicates with mediaUrl/mediaUrls
                  if (!attachments.some((a) => a.path === path)) {
                    attachments.push({ path: path });
                  }
                }

                if (attachments.length > 0) {
                  ctx.log?.info?.(
                    `[${account.accountId}] Sending reply to ${fromEmail} with ${attachments.length} attachment(s)`,
                  );
                } else {
                  ctx.log?.info?.(`[${account.accountId}] Sending reply to ${fromEmail}`);
                }

                await sendEmail(
                  account.accountId || "default",
                  fromEmail,
                  subject,
                  replyText,
                  messageId,
                  attachments.length > 0 ? attachments : undefined,
                );
              },
              onError: (err: any, info: any) => {
                ctx.log?.error?.(`[${account.accountId}] Email reply failed: ${String(err)}`);
              },
            },
            replyOptions: {
              disableBlockStreaming: true, // Email doesn't support streaming
            },
          });

          ctx.log?.info?.(`[${account.accountId}] Email processed successfully`);
        } catch (error: any) {
          // Log detailed error information
          const errorMsg = error?.message || String(error);
          const errorStack = error?.stack || "";
          const errorDetails = error?.toString() || String(error);

          ctx.log?.error?.(`[${account.accountId}] Error processing email from ${fromEmail}:`);
          ctx.log?.error?.(
            `[${account.accountId}] Error type: ${error?.constructor?.name || "Unknown"}`,
          );
          ctx.log?.error?.(`[${account.accountId}] Error message: ${errorMsg}`);
          if (errorStack) {
            ctx.log?.error?.(
              `[${account.accountId}] Stack trace: ${errorStack.split("\n").slice(0, 5).join(" | ")}`,
            );
          }
          ctx.log?.error?.(`[${account.accountId}] Full error: ${errorDetails}`);

          // Send error notification to sender only if not a sending error
          if (!errorMsg.includes("send") && !errorMsg.includes("SMTP")) {
            try {
              const errorMessage =
                "Sorry, there was an error processing your request. Please try again later.";
              await sendEmail(
                account.accountId || "default",
                fromEmail,
                subject,
                errorMessage,
                messageId,
              );
            } catch (sendError) {
              ctx.log?.error?.(
                `[${account.accountId}] Failed to send error notification: ${String(sendError)}`,
              );
            }
          }
        }
      },
    );

    // Return cleanup function
    return () => {
      ctx.log?.info?.(`[${account.accountId}] Stopping email channel`);
      stopEmail(account.accountId || "default");
    };
  },
};

const emailPlugin: ChannelPlugin<EmailAccount> = {
  id: "email",

  meta: {
    id: "email",
    label: "Email",
    selectionLabel: "Email (IMAP/SMTP)",
    docsPath: "/channels/email",
    blurb: "Send and receive email via IMAP/SMTP servers.",
    aliases: ["mail", "smtp"],
    // Note: discovery metadata available if using enhanced SDK (PR #24087)
    // Uncomment the following if PR is merged:
    // discovery: {
    //   category: "email",
    //   keywords: ["email", "imap", "smtp", "messaging"],
    //   maturity: "experimental",
    //   docsLink: "https://github.com/guxiaobo/openclaw/tree/feature/email-channel/extensions/email-channel",
    //   author: "OpenClaw Community",
    // },
  },

  capabilities: {
    chatTypes: ["direct"],
  },

  config: {
    listAccountIds: (cfg: OpenClawConfig) => {
      const accounts = cfg.channels?.email?.accounts;
      return accounts ? Object.keys(accounts) : [];
    },

    resolveAccount: (cfg: OpenClawConfig, accountId?: string | null) => {
      const accounts = cfg.channels?.email?.accounts;
      const account = accounts?.[accountId || "default"] || accounts?.default || {};
      return {
        accountId: accountId || "default",
        enabled: account.enabled ?? true,
        ...account,
      } as EmailAccount;
    },

    isConfigured: (account: EmailAccount) => {
      return Boolean(
        account.imap?.host &&
        account.imap?.port &&
        account.imap?.user &&
        account.imap?.password &&
        account.smtp?.host &&
        account.smtp?.port &&
        account.smtp?.user &&
        account.smtp?.password,
      );
    },
  },

  // Config schema is optional - channel uses config directly
  // TODO: Add Zod schema for config validation
  // configSchema: buildChannelConfigSchema(...),

  gateway: emailGatewayAdapter,

  // TODO: Implement proper security adapter
  // security: {
  //   resolveDmPolicy: ...
  // },

  outbound: {
    deliveryMode: "direct",
    sendText: async ({ text, to }) => {
      // Parse target format: "accountId:recipientEmail|subject|messageId"
      const match = to.match(/^([^:]+):([^|]+)\|([^|]+)\|(.+)$/);
      if (!match) {
        return {
          ok: false,
          error: "Invalid email target format",
          channel: "email" as const,
          messageId: `error-${Date.now()}`,
        };
      }

      const [, accountId, recipientEmail, subject, messageId] = match;
      const success = await sendEmail(accountId, recipientEmail, subject, text, messageId);

      return {
        ok: success,
        channel: "email" as const,
        messageId: success ? messageId : `failed-${Date.now()}`,
      };
    },
  },

  messaging: {
    normalizeTarget: (raw: string) => {
      // If already in correct format, return as-is
      if (raw.match(/^[^:]+:[^|]+\|[^|]+\|.+$/)) {
        return raw;
      }

      // Strip "email:" prefix if present
      let emailAddr = raw.startsWith("email:") ? raw.substring(6) : raw;

      // If it looks like an email address, format it
      if (emailAddr.includes("@") && !emailAddr.includes("|")) {
        const contextKey = `email:${emailAddr}`;
        const context = emailContexts.get(contextKey);
        if (context) {
          return `default:${emailAddr}|${context.subject}|${context.messageId}`;
        }
        return `default:${emailAddr}|No Subject|no-message-id`;
      }

      return raw;
    },
  },
};

export { emailPlugin };
