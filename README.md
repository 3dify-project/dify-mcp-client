# dify-mcp-client
`MCP Client` as Agent Strategy Plugin.
> [!IMPORTANT]
> Dify is not `MCP Server` but `MCP Host`. 

![showcase](./_assets/arxiv_mcp_server_test.png)

## How it works
Currently, each `MCP client` (ReAct Agent) node can connect a stdio `MCP server`.
1.  `Tool`, `Resource`, `Prompt` lists are converted into Dify Tools.
2.   Your selected LLM can see their `name`, `description`, `argument type`
3.   The LLM calls Tools based on the ReAct loop (Reason → Act → Observe).

> [!NOTE]
> Most of the code in this repository contains the following files.
> #### Dify Official Plugins
> https://github.com/langgenius/dify-official-plugins/tree/main/agent-strategies/cot_agent

## What I did
- Copied `ReAct.py` and renamed file as `mcpReAct.py`
- Added `config_json` GUI input field by editing `mcpReAct.yaml` and `class mcpReActParams()` 

### in mcpReAct.py, I added
- New 12 functions for MCP 
- `__init__()` for initializing `AsyncExitStack` and `event loop`
- Some codes in `_handle_invoke_action()` for MCP 
- MCP setup and cleanup in `_invoke()`
> [!IMPORTANT]
> ReAct while loop is as they are


## Useful GitHub repositories for developers

#### Dify Plugin SDKs
https://github.com/langgenius/dify-plugin-sdks

#### MCP Python SDK
https://github.com/modelcontextprotocol/python-sdk
<br>

> [!TIP]
> Especially useful following MCP client example<br>
> https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/clients/simple-chatbot/mcp_simple_chatbot/main.py<br>

> [!NOTE]
> Dify plugin has `requirements.txt` which automatically installs python modules.<br>
> I include `mcp` in it, so you don't need to download the MCP SDK separately.

## Before Start
> [!CAUTION]
> This plugin does **not** implement a **human-in-the-loop** mechanism by default, so connect **reliable mcp server only**.<br>
> To avoid it, decrease `max itereations`(default:`3`) to `1`, and use this Agent node repeatedly in Chatflow.<br>
> However, agent memory is reset by the end of Workflow, so use `Conversaton Variable` to save history and pass to QUERY.  
> Don't forget to add a phrase such as
> *"ask for user's permission when calling tools"* in INSTRUCTION.

> [!WARNING]
> - The Tools field should not be left blank. so **select Dify tools** like "current time".
> - The SSE connection is not supported

## How to use plugin

### Install plugin from GitHub (Easy)
- Dify > PLUGINS > + Install plugin > INSTALL FROM > GitHub
- GitHub repository https://github.com/3dify-project/dify-mcp-client/
![difyUI1](./_assets/plugin_install_online.png)

### Install plugin from GitHub ()
- Go to Releases https://github.com/3dify-project/dify-mcp-client/releases
- Select suitable version of `.difypkg`
- Dify > PLUGINS > + Install plugin > INSTALL FROM > Local Package File
![difyUI2](./_assets/plugin_install_offline.png)
