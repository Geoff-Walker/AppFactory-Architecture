import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { getSecret, listSecrets } from "./infisical.js";

const server = new Server(
  { name: "infisical-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// Tell Claude what tools exist and what arguments they take
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "get_secret",
      description:
        "Retrieve a single secret value from Infisical by name. Use list_secrets first if you are unsure what secrets exist.",
      inputSchema: {
        type: "object",
        properties: {
          name: {
            type: "string",
            description: "The secret key name, e.g. POSTGRES_PASSWORD",
          },
          project: {
            type: "string",
            description: "The Infisical project slug, e.g. greensaber",
          },
          environment: {
            type: "string",
            description: "The environment slug, e.g. staging or prod",
          },
        },
        required: ["name", "project", "environment"],
      },
    },
    {
      name: "list_secrets",
      description:
        "List the names of all secrets in an Infisical project and environment. Returns key names only — not values.",
      inputSchema: {
        type: "object",
        properties: {
          project: {
            type: "string",
            description: "The Infisical project slug, e.g. greensaber",
          },
          environment: {
            type: "string",
            description: "The environment slug, e.g. staging or prod",
          },
        },
        required: ["project", "environment"],
      },
    },
  ],
}));

// Handle tool calls from Claude
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    if (name === "get_secret") {
      const { name: secretName, project, environment } = args as {
        name: string;
        project: string;
        environment: string;
      };
      const value = await getSecret(secretName, project, environment);
      return {
        content: [{ type: "text", text: value }],
      };
    }

    if (name === "list_secrets") {
      const { project, environment } = args as {
        project: string;
        environment: string;
      };
      const keys = await listSecrets(project, environment);
      return {
        content: [{ type: "text", text: keys.join("\n") }],
      };
    }

    throw new Error(`Unknown tool: ${name}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      content: [{ type: "text", text: `Error: ${message}` }],
      isError: true,
    };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
