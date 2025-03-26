import os
import asyncio
import threading
import ast
import json
import time
from collections.abc import Generator, Mapping
from typing import Any, Optional, cast
from contextlib import AsyncExitStack

from dify_plugin.entities import I18nObject
from dify_plugin.entities.agent import AgentInvokeMessage
from dify_plugin.entities.model.llm import LLMModelConfig, LLMUsage
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessage,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.entities.tool import (
    LogMetadata,
    ToolInvokeMessage,
    ToolParameter,
    ToolProviderType,
    ToolDescription,
)
from dify_plugin.interfaces.agent import (
    AgentModelConfig,
    AgentScratchpadUnit,
    AgentStrategy,
    ToolEntity,
    AgentToolIdentity,
)
from output_parser.cot_output_parser import CotAgentOutputParser
from prompt.template import REACT_PROMPT_TEMPLATES
from pydantic import BaseModel, Field

ignore_observation_providers = ["wenxin"]

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.types import Tool, Resource, Prompt, ListToolsResult, ListResourcesResult, ListPromptsResult


class mcpReActParams(BaseModel):
    query: str
    instruction: str | None
    model: AgentModelConfig
    tools: list[ToolEntity] | None
    inputs: dict[str, Any] = {}
    maximum_iterations: int = 3
    config_json: str | None # AgentsStrategyType does not support "object" type so I use "str" instead of dict[str, Any]


class AgentPromptEntity(BaseModel):
    """
    Agent Prompt Entity.
    """

    first_prompt: str
    next_iteration: str


class ToolInvokeMeta(BaseModel):
    """
    Tool invoke meta
    """

    time_cost: float = Field(..., description="The time cost of the tool invoke")
    error: Optional[str] = None
    tool_config: Optional[dict] = None

    @classmethod
    def empty(cls) -> "ToolInvokeMeta":
        """
        Get an empty instance of ToolInvokeMeta
        """
        return cls(time_cost=0.0, error=None, tool_config={})

    @classmethod
    def error_instance(cls, error: str) -> "ToolInvokeMeta":
        """
        Get an instance of ToolInvokeMeta with error
        """
        return cls(time_cost=0.0, error=error, tool_config={})

    def to_dict(self) -> dict:
        return {
            "time_cost": self.time_cost,
            "error": self.error,
            "tool_config": self.tool_config,
        }


class mcpReActAgentStrategy(AgentStrategy):
    def __init__(self, session):
        super().__init__(session)
        self.exit_stack = AsyncExitStack()
        self.mcp_sessions = {} # dict[str, ClientSession]  # store multiple MCP sessions
        # added shared "event loop" and user counter as class variables
        self._shared_loop = None
        self._loop_users = 0
        self._loop_lock = threading.Lock()  # lock for thread safety

    def _invoke(self, parameters: dict[str, Any]) -> Generator[AgentInvokeMessage]:
        react_params = mcpReActParams(**parameters)
        query = react_params.query
        model = react_params.model

        mcp_config_json = ast.literal_eval(react_params.config_json)

        agent_scratchpad = []
        history_prompt_messages: list[PromptMessage] = []
        current_session_messages = []
        self._organize_historic_prompt_messages(
            history_prompt_messages, current_session_messages=current_session_messages
        )
        tools = react_params.tools
        tool_instances = {tool.identity.name: tool for tool in tools} if tools else {}
        react_params.model.completion_params = (
            react_params.model.completion_params or {}
        )
        # check model mode
        stop = (
            react_params.model.completion_params.get("stop", [])
            if react_params.model.completion_params
            else []
        )

        if (
            "Observation" not in stop
            and model.provider not in ignore_observation_providers
        ):
            stop.append("Observation")
        # init instruction
        inputs = react_params.inputs
        instruction = react_params.instruction or ""
        self._instruction = self._fill_in_inputs_from_external_data_tools(
            instruction, inputs
        )

        # convert tools into ModelRuntime Tool format
        prompt_messages_tools = self._init_prompt_tools(tools)
        self._prompt_messages_tools = prompt_messages_tools

        ############### MCP setup ###############
        # exsample structure of mcp_config_json
        # {
        #     "mcpservers": {
        #         {
        #             "name_of_mcpserver1": {
        #                 "command": "npx",
        #                 "args": ["arg1", "arg2"],
        #                 "env": {"API_KEY": "value"}
        #             },
        #             "name_of_mcpserver2": {
        #                 "url": "http://localhost:3000/sse",
        #             }
        #         }
        #     }
        # }

        # itarate over the number of "mcpServer" in configjson
        for mcp_server_name, mcp_server_cmd_or_url in mcp_config_json["mcpServers"].items():
            # exsample structure of mcp_server_cmd_or_url
            #
            #         "name_of_mcpserver1": {
            #             "command": "npx",
            #             "args": ["arg1", "arg2"]
            #         }
            if mcp_server_cmd_or_url.get("command"): # stdio (standard I/O)
                # connect to MCP server
                mcp_tool_list, mcp_resource_list, mcp_prompt_list, self.mcp_sessions[mcp_server_name] = self._run_async(self._setup_stdio_mcp(mcp_server_cmd_or_url))

            elif mcp_server_cmd_or_url.get("url"): # SSE
                mcp_tool_list, mcp_resource_list, mcp_prompt_list, self.mcp_sessions[mcp_server_name] = self._run_async(self._setup_sse_mcp(mcp_server_cmd_or_url))

            # convert {tools, resources, prompts} into Dify-plugin's ToolEntity format
            mcp_list = self._convert_mcp_components_to_tool_entities(mcp_server_name, mcp_tool_list, mcp_resource_list, mcp_prompt_list)
    
            # convert mcp {tool, resource, prompt} list into ModelRuntime Tool format
            prompt_messages_tools = self._init_prompt_tools(mcp_list)
            self._prompt_messages_tools += prompt_messages_tools

            # add MCP tools to tool_instances mapping
            for tool in mcp_list:
                tool_instances[tool.identity.name] = tool

        #########################################

        iteration_step = 1
        max_iteration_steps = react_params.maximum_iterations

        run_agent_state = True
        llm_usage: dict[str, Optional[LLMUsage]] = {"usage": None}
        final_answer = ""
        prompt_messages = []
        while run_agent_state and iteration_step <= max_iteration_steps:
            # continue to run until there is not any tool call
            run_agent_state = False
            round_started_at = time.perf_counter()
            round_log = self.create_log_message(
                label=f"ROUND {iteration_step}",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                },
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield round_log
            if iteration_step == max_iteration_steps:
                # the last iteration, remove all tools
                self._prompt_messages_tools = []

            message_file_ids: list[str] = []

            # recalc llm max tokens
            prompt_messages = self._organize_prompt_messages(agent_scratchpad, query)
            if model.completion_params:
                self.recalc_llm_max_tokens(
                    model.entity, prompt_messages, model.completion_params
                )
            # invoke model
            chunks = self.session.model.llm.invoke(
                model_config=LLMModelConfig(**model.model_dump(mode="json")),
                prompt_messages=prompt_messages,
                stream=True,
                stop=stop,
            )

            usage_dict = {}
            react_chunks = CotAgentOutputParser.handle_react_stream_output(
                chunks, usage_dict
            )
            scratchpad = AgentScratchpadUnit(
                agent_response="",
                thought="",
                action_str="",
                observation="",
                action=None,
            )

            model_started_at = time.perf_counter()
            model_log = self.create_log_message(
                label=f"{model.model} Thought",
                data={},
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                },
                parent=round_log,
                status=ToolInvokeMessage.LogMessage.LogStatus.START,
            )
            yield model_log

            for chunk in react_chunks:
                if isinstance(chunk, AgentScratchpadUnit.Action):
                    action = chunk
                    # detect action
                    assert scratchpad.agent_response is not None
                    scratchpad.agent_response += json.dumps(chunk.model_dump())

                    scratchpad.action_str = json.dumps(chunk.model_dump())
                    scratchpad.action = action
                else:
                    scratchpad.agent_response = scratchpad.agent_response or ""
                    scratchpad.thought = scratchpad.thought or ""
                    scratchpad.agent_response += chunk
                    scratchpad.thought += chunk
            scratchpad.thought = (
                scratchpad.thought.strip()
                if scratchpad.thought
                else "I am thinking about how to help you"
            )
            agent_scratchpad.append(scratchpad)

            # get llm usage
            if "usage" in usage_dict:
                if usage_dict["usage"] is not None:
                    self.increase_usage(llm_usage, usage_dict["usage"])
            else:
                usage_dict["usage"] = LLMUsage.empty_usage()

            action = (
                scratchpad.action.to_dict()
                if scratchpad.action
                else {"action": scratchpad.agent_response}
            )

            yield self.finish_log_message(
                log=model_log,
                data={"thought": scratchpad.thought, **action},
                metadata={
                    LogMetadata.STARTED_AT: model_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - model_started_at,
                    LogMetadata.PROVIDER: model.provider,
                    LogMetadata.TOTAL_PRICE: usage_dict["usage"].total_price
                    if usage_dict["usage"]
                    else 0,
                    LogMetadata.CURRENCY: usage_dict["usage"].currency
                    if usage_dict["usage"]
                    else "",
                    LogMetadata.TOTAL_TOKENS: usage_dict["usage"].total_tokens
                    if usage_dict["usage"]
                    else 0,
                },
            )
            if not scratchpad.action:
                final_answer = scratchpad.thought
            else:
                if scratchpad.action.action_name.lower() == "final answer":
                    # action is final answer, return final answer directly
                    try:
                        if isinstance(scratchpad.action.action_input, dict):
                            final_answer = json.dumps(scratchpad.action.action_input)
                        elif isinstance(scratchpad.action.action_input, str):
                            final_answer = scratchpad.action.action_input
                        else:
                            final_answer = f"{scratchpad.action.action_input}"
                    except json.JSONDecodeError:
                        final_answer = f"{scratchpad.action.action_input}"
                        self._run_async(self._cleanup_mcp())
                else:
                    run_agent_state = True
                    # action is tool call, invoke tool
                    tool_call_started_at = time.perf_counter()
                    tool_name = scratchpad.action.action_name
                    tool_call_log = self.create_log_message(
                        label=f"CALL {tool_name}",
                        data={},
                        metadata={
                            LogMetadata.STARTED_AT: time.perf_counter(),
                            LogMetadata.PROVIDER: tool_instances[
                                tool_name
                            ].identity.provider
                            if tool_instances.get(tool_name)
                            else "",
                        },
                        parent=round_log,
                        status=ToolInvokeMessage.LogMessage.LogStatus.START,
                    )
                    yield tool_call_log
                    tool_invoke_response, tool_invoke_parameters = (
                        self._handle_invoke_action(
                            action=scratchpad.action,
                            tool_instances=tool_instances,
                            message_file_ids=message_file_ids,
                        )
                    )
                    scratchpad.observation = tool_invoke_response
                    scratchpad.agent_response = tool_invoke_response
                    yield self.finish_log_message(
                        log=tool_call_log,
                        data={
                            "tool_name": tool_name,
                            "tool_call_args": tool_invoke_parameters,
                            "output": tool_invoke_response,
                        },
                        metadata={
                            LogMetadata.STARTED_AT: tool_call_started_at,
                            LogMetadata.PROVIDER: tool_instances[
                                tool_name
                            ].identity.provider
                            if tool_instances.get(tool_name)
                            else "",
                            LogMetadata.FINISHED_AT: time.perf_counter(),
                            LogMetadata.ELAPSED_TIME: time.perf_counter()
                            - tool_call_started_at,
                        },
                    )

                # update prompt tool message
                for prompt_tool in self._prompt_messages_tools:
                    self.update_prompt_message_tool(
                        tool_instances[prompt_tool.name], prompt_tool
                    )
            yield self.finish_log_message(
                log=round_log,
                data={
                    "action_name": scratchpad.action.action_name
                    if scratchpad.action
                    else "",
                    "action_input": scratchpad.action.action_input
                    if scratchpad.action
                    else "",
                    "thought": scratchpad.thought,
                    "observation": scratchpad.observation,
                },
                metadata={
                    LogMetadata.STARTED_AT: round_started_at,
                    LogMetadata.FINISHED_AT: time.perf_counter(),
                    LogMetadata.ELAPSED_TIME: time.perf_counter() - round_started_at,
                    LogMetadata.TOTAL_PRICE: usage_dict["usage"].total_price
                    if usage_dict["usage"]
                    else 0,
                    LogMetadata.CURRENCY: usage_dict["usage"].currency
                    if usage_dict["usage"]
                    else "",
                    LogMetadata.TOTAL_TOKENS: usage_dict["usage"].total_tokens
                    if usage_dict["usage"]
                    else 0,
                },
            )
            iteration_step += 1

        yield self.create_text_message(final_answer)
        yield self.create_json_message(
            {
                "execution_metadata": {
                    LogMetadata.TOTAL_PRICE: llm_usage["usage"].total_price
                    if llm_usage["usage"] is not None
                    else 0,
                    LogMetadata.CURRENCY: llm_usage["usage"].currency
                    if llm_usage["usage"] is not None
                    else "",
                    LogMetadata.TOTAL_TOKENS: llm_usage["usage"].total_tokens
                    if llm_usage["usage"] is not None
                    else 0,
                }
            }
        )
        ############### MCP cleanup ###############
        try:
            self._shared_loop.close()
            print("Cleanup shared event loop")
        except Exception as e:
            print(f"Error when closing loop: {e}")
        finally:
            asyncio.set_event_loop(None)
            self._shared_loop = None

        print("MCP Agent Strategy finished")
        self._run_async(self._cleanup_mcp()) # need to aclose() because not using with open
        ###########################################


    def _organize_system_prompt(self) -> SystemPromptMessage:
        """
        Organize system prompt
        """

        prompt_entity = AgentPromptEntity(
            first_prompt=REACT_PROMPT_TEMPLATES["english"]["chat"]["prompt"],
            next_iteration=REACT_PROMPT_TEMPLATES["english"]["chat"][
                "agent_scratchpad"
            ],
        )
        if not prompt_entity:
            self._run_async(self._cleanup_mcp())
            raise ValueError("Agent prompt configuration is not set")
        first_prompt = prompt_entity.first_prompt

        system_prompt = (
            first_prompt.replace("{{instruction}}", self._instruction)
            .replace(
                "{{tools}}",
                json.dumps(
                    [
                        tool.model_dump(mode="json")
                        for tool in self._prompt_messages_tools
                    ]
                ),
            )
            .replace(
                "{{tool_names}}",
                ", ".join([tool.name for tool in self._prompt_messages_tools]),
            )
        )

        return SystemPromptMessage(content=system_prompt)

    def _organize_user_query(
        self, query, prompt_messages: list[PromptMessage]
    ) -> list[PromptMessage]:
        """
        Organize user query
        """
        prompt_messages.append(UserPromptMessage(content=query))

        return prompt_messages

    def _organize_prompt_messages(
        self, agent_scratchpad: list, query: str
    ) -> list[PromptMessage]:
        """
        Organize
        """
        # organize system prompt
        system_message = self._organize_system_prompt()

        # organize current assistant messages
        agent_scratchpad = agent_scratchpad
        if not agent_scratchpad:
            assistant_messages = []
        else:
            assistant_message = AssistantPromptMessage(content="")
            assistant_message.content = (
                ""  # FIXME: type check tell mypy that assistant_message.content is str
            )
            for unit in agent_scratchpad:
                if unit.is_final():
                    assert isinstance(assistant_message.content, str)
                    assistant_message.content += f"Final Answer: {unit.agent_response}"
                else:
                    assert isinstance(assistant_message.content, str)
                    assistant_message.content += f"Thought: {unit.thought}\n\n"
                    if unit.action_str:
                        assistant_message.content += f"Action: {unit.action_str}\n\n"
                    if unit.observation:
                        assistant_message.content += (
                            f"Observation: {unit.observation}\n\n"
                        )

            assistant_messages = [assistant_message]

        # query messages
        query_messages = self._organize_user_query(query, [])

        if assistant_messages:
            # organize historic prompt messages
            historic_messages = self._organize_historic_prompt_messages(
                [
                    system_message,
                    *query_messages,
                    *assistant_messages,
                    UserPromptMessage(content="continue"),
                ]
            )
            messages = [
                system_message,
                *historic_messages,
                *query_messages,
                *assistant_messages,
                UserPromptMessage(content="continue"),
            ]
        else:
            # organize historic prompt messages
            historic_messages = self._organize_historic_prompt_messages(
                [system_message, *query_messages]
            )
            messages = [system_message, *historic_messages, *query_messages]

        # join all messages
        return messages

    def _handle_invoke_action(
        self,
        action: AgentScratchpadUnit.Action,
        tool_instances: Mapping[str, ToolEntity],
        message_file_ids: list[str],
    ) -> tuple[str, dict[str, Any] | str]:
        """
        handle invoke action
        :param action: action
        :param tool_instances: tool instances
        :param message_file_ids: message file ids
        :param trace_manager: trace manager
        :return: observation, meta
        """
        # action is tool call, invoke tool
        tool_call_name = action.action_name
        tool_call_args = action.action_input
        tool_instance = tool_instances.get(tool_call_name)

        if not tool_instance:
            answer = f"there is not a tool named {tool_call_name}"
            return answer, tool_call_args

        if isinstance(tool_call_args, str):
            try:
                tool_call_args = json.loads(tool_call_args)
            except json.JSONDecodeError as e:
                params = [
                    param.name
                    for param in tool_instance.parameters
                    if param.form == ToolParameter.ToolParameterForm.LLM
                ]
                if len(params) > 1:
                    self._run_async(self._cleanup_mcp())
                    raise ValueError("tool call args is not a valid json string") from e
                tool_call_args = {params[0]: tool_call_args} if len(params) == 1 else {}
                self._run_async(self._cleanup_mcp())

        tool_invoke_parameters = {**tool_instance.runtime_parameters, **tool_call_args}

        ########## MCP implementation ##########
        # judge if it is MCP tool
        is_mcp_tool, mcp_action_type, action_name, mcp_server_name = self._parse_mcp_tool_name(tool_call_name)
        
        # if it is MCP action, invoke it with MCP session
        if is_mcp_tool:
            try:
                # execute async function synchronously
                action_result = self._run_async(self._invoke_mcp_action(
                    self.mcp_sessions[mcp_server_name], mcp_action_type, action_name, tool_invoke_parameters
                ))
                #print(f"result: {action_result}")
    
                result = self._format_mcp_result(mcp_action_type, action_result)
                    
            except Exception as e:
                print(f"MCP Action Execution Error: {str(e)}")
                result = f"MCP Action Execution Error: {str(e)}"
            
            return result, tool_invoke_parameters
        #########################################

        try:
            tool_invoke_responses = self.session.tool.invoke(
                provider_type=ToolProviderType(tool_instance.provider_type),
                provider=tool_instance.identity.provider,
                tool_name=tool_instance.identity.name,
                parameters=tool_invoke_parameters,
            )
            result = ""
            for response in tool_invoke_responses:
                if response.type == ToolInvokeMessage.MessageType.TEXT:
                    result += cast(ToolInvokeMessage.TextMessage, response.message).text
                elif response.type == ToolInvokeMessage.MessageType.LINK:
                    result += (
                        f"result link: {cast(ToolInvokeMessage.TextMessage, response.message).text}."
                        + " please tell user to check it."
                    )
                elif response.type in {
                    ToolInvokeMessage.MessageType.IMAGE_LINK,
                    ToolInvokeMessage.MessageType.IMAGE,
                }:
                    result += (
                        "image has been created and sent to user already, "
                        + "you do not need to create it, just tell the user to check it now."
                    )
                elif response.type == ToolInvokeMessage.MessageType.JSON:
                    text = json.dumps(
                        cast(
                            ToolInvokeMessage.JsonMessage, response.message
                        ).json_object,
                        ensure_ascii=False,
                    )
                    result += f"tool response: {text}."
                else:
                    result += f"tool response: {response.message!r}."
        except Exception as e:
            print(f"tool invoke error: {str(e)}")
            result = f"tool invoke error: {str(e)}"

        return result, tool_invoke_parameters

    def _convert_dict_to_action(self, action: dict) -> AgentScratchpadUnit.Action:
        """
        convert dict to action
        """
        return AgentScratchpadUnit.Action(
            action_name=action["action"], action_input=action["action_input"]
        )

    def _fill_in_inputs_from_external_data_tools(
        self, instruction: str, inputs: Mapping[str, Any]
    ) -> str:
        """
        fill in inputs from external data tools
        """
        for key, value in inputs.items():
            try:
                instruction = instruction.replace(f"{{{{{key}}}}}", str(value))
            except Exception:
                continue

        return instruction

    def _format_assistant_message(
        self, agent_scratchpad: list[AgentScratchpadUnit]
    ) -> str:
        """
        format assistant message
        """
        message = ""
        for scratchpad in agent_scratchpad:
            if scratchpad.is_final():
                message += f"Final Answer: {scratchpad.agent_response}"
            else:
                message += f"Thought: {scratchpad.thought}\n\n"
                if scratchpad.action_str:
                    message += f"Action: {scratchpad.action_str}\n\n"
                if scratchpad.observation:
                    message += f"Observation: {scratchpad.observation}\n\n"

        return message

    def _organize_historic_prompt_messages(
        self,
        history_prompt_messages: list[PromptMessage],
        current_session_messages: list[PromptMessage] | None = None,
    ) -> list[PromptMessage]:
        """
        organize historic prompt messages
        """
        result: list[PromptMessage] = []
        scratchpads: list[AgentScratchpadUnit] = []
        current_scratchpad: AgentScratchpadUnit | None = None

        for message in history_prompt_messages:
            if isinstance(message, AssistantPromptMessage):
                if not current_scratchpad:
                    assert isinstance(message.content, str)
                    current_scratchpad = AgentScratchpadUnit(
                        agent_response=message.content,
                        thought=message.content
                        or "I am thinking about how to help you",
                        action_str="",
                        action=None,
                        observation=None,
                    )
                    scratchpads.append(current_scratchpad)
                if message.tool_calls:
                    try:
                        current_scratchpad.action = AgentScratchpadUnit.Action(
                            action_name=message.tool_calls[0].function.name,
                            action_input=json.loads(
                                message.tool_calls[0].function.arguments
                            ),
                        )
                        current_scratchpad.action_str = json.dumps(
                            current_scratchpad.action.to_dict()
                        )
                    except Exception:
                        pass
            elif isinstance(message, ToolPromptMessage):
                if current_scratchpad:
                    assert isinstance(message.content, str)
                    current_scratchpad.observation = message.content
                else:
                    self._run_async(self._cleanup_mcp())
                    raise NotImplementedError("expected str type")
            elif isinstance(message, UserPromptMessage):
                if scratchpads:
                    result.append(
                        AssistantPromptMessage(
                            content=self._format_assistant_message(scratchpads)
                        )
                    )
                    scratchpads = []
                    current_scratchpad = None

                result.append(message)

        if scratchpads:
            result.append(
                AssistantPromptMessage(
                    content=self._format_assistant_message(scratchpads)
                )
            )

        return current_session_messages or []

######################## following methods are MCP specific ########################

    def _parse_mcp_tool_name(self, tool_name: str) -> tuple[bool, str, str, str]:
        """Determine if the tool name is an MCP tool, and return the type and actual name

        Args:
            tool_name: name as Dify tool 

        Returns:
            (is_mcp_tool, action_type, action_name, mcp_server_name)
        """
        if "_mcp_tool_" in tool_name:
            # example: xxx_mcp_tool_yyy 
            #            |  split  |
            # [0] ->  xxx: MCP server name,
            # [1] ->  yyy: Actual action name
            return True, "tool", tool_name.split("_mcp_tool_")[1], tool_name.split("_mcp_tool_")[0]
        elif "_mcp_resource_" in tool_name:
            return True, "resource", tool_name.split("_mcp_resource_")[1], tool_name.split("_mcp_resource_")[0]
        elif "_mcp_prompt_" in tool_name:
            return True, "prompt", tool_name.split("_mcp_prompt_")[1], tool_name.split("_mcp_prompt_")[0]
        return False, "", "", ""
    
    def _format_mcp_result(self, action_type: str, action_result: Any) -> str:
        """Format the MCP execution result as a string
        
        Args:
            action_type: action type ('tool', 'resource', 'prompt')
            action_result: execution result object
        
        Returns:
            formatted result string
        """
        result = ""
        
        if action_type == "tool":
            if hasattr(action_result, 'content'):
                for content_item in action_result.content:
                    if hasattr(content_item, 'text'):
                        result += content_item.text
                    elif hasattr(content_item, 'resource'):
                        result += f"[Resource: {content_item.resource.uri}]"
                    else:
                        result += str(content_item)
            else:
                result = str(action_result)
                
        elif action_type == "resource":
            if hasattr(action_result, 'contents'):
                for content in action_result.contents:
                    if hasattr(content, 'text'):
                        result += f"Resource content ({content.mimeType or 'unknown'}): {content.text}"
                    elif hasattr(content, 'blob'):
                        result += f"Binary resource ({content.mimeType or 'unknown'}) of size {len(content.blob)} bytes"
            else:
                result = str(action_result)
                
        elif action_type == "prompt":
            if hasattr(action_result, 'messages'):
                for msg in action_result.messages:
                    content_text = getattr(msg.content, 'text', str(msg.content))
                    result += f"{msg.role}: {content_text}\n"
                result = result.strip()
            else:
                result = str(action_result)
        else:
            result = str(action_result)
            
        return result

    async def _invoke_mcp_action(
        self, 
        mcp_session: ClientSession, 
        action_type: str,
        name: str, 
        arguments: dict
    ) -> Any:
        """Asynchronous function to handle various calls to the MCP server
        
        param mcp_session:  MCP client session
        param action_type:  call type ('tool', 'resource', 'prompt')
        param name:  tool/resource/prompt name
        param arguments:  arguments
        return:  call result
        """
        try:
            if action_type == 'tool':
                return await mcp_session.call_tool(name, arguments=arguments)
            elif action_type == 'resource':
                uri = arguments.get('uri', name)
                return await mcp_session.read_resource(uri)
            elif action_type == 'prompt':
                prompt_name = arguments.get('prompt_name', name)
                prompt_args = {k: v for k, v in arguments.items() if k != 'prompt_name'}
                return await mcp_session.get_prompt(prompt_name, arguments=prompt_args)
            else:
                return f"Unsupported MCP action type: {action_type}"
        except Exception as e:
            await self._cleanup_mcp()
            return f"MCP Call Error: {str(e)}"

    def _get_mcp_server_params_from_config(self, config_json: dict) -> StdioServerParameters:
        # exsample structure of config

        # "filesystem": {
        #   "command": "npx",
        #   "args": [
        #     "-y",
        #     "@modelcontextprotocol/server-filesystem",
        #     "C:\\Users\\username\\Desktop",
        #     "C:\\Users\\username\\Downloads"
        #   ]
        # }
        command = config_json["command"]
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")

        server_params = StdioServerParameters(
            command=command,
            args=config_json["args"],
            env={**os.environ, **config_json["env"]}
            if config_json.get("env")
            else None,
        )

        return server_params
    
    def _get_mcp_server_url_from_config(self, config_json: dict) -> str: 
        """
           "env" is not supported.
           Additional env is supporesd to be set outside the Dify
           when awaking SSE MCP server.
        """
        # exsample structure of config

        # "sse_server_name": {
        #   "url": "http://localhost:3000/sse",
        # }
        url = config_json["url"]
        if url is None:
            raise ValueError("The URL must be a valid string and cannot be None.")
        
        return url
    
    async def _get_mcp_action_list(self, mcp_session: ClientSession) -> tuple[ListToolsResult, ListResourcesResult, ListPromptsResult]:
        try:
            # call only the methods that the server has safely
            try:
                tool_list = await mcp_session.list_tools()
            except Exception:
                tool_list = []  # use an empty list if not supported
                
            try:
                resource_list = await mcp_session.list_resources()
            except Exception:
                resource_list = []
                
            try:
                prompt_list = await mcp_session.list_prompts()
            except Exception:
                prompt_list = []
                
            return tool_list, resource_list, prompt_list
        
        except Exception as e:
            await self._cleanup_mcp()
            raise ValueError(f"Failed to get MCP <tool, resource, prompt> list: {e}")

    async def _setup_stdio_mcp(self, config_json: dict) -> tuple[ListToolsResult, ListResourcesResult, ListPromptsResult, ClientSession]:
        """Establish stdio MCP client connection (called in _invoke)"""
        server_params = self._get_mcp_server_params_from_config(config_json)
        try:
            transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
            read, write = transport
            mcp_session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await mcp_session.initialize()
            tool_list, resource_list, prompt_list = await self._get_mcp_action_list(mcp_session)
                
            return tool_list, resource_list, prompt_list, mcp_session
            
        except Exception as e:
            await self._cleanup_mcp()
            raise ValueError(f"Failed to connect to MCP server with stdio: {e}")
    
    async def _setup_sse_mcp(self, config_json: dict) -> tuple[ListToolsResult, ListResourcesResult, ListPromptsResult, ClientSession]:
        """Connect to an MCP server running with SSE transport"""
        mcp_server_url = self._get_mcp_server_url_from_config(config_json)
        try:
            sse_transport = await self.exit_stack.enter_async_context(sse_client(url=mcp_server_url))
            read, write = sse_transport
            mcp_session = await self.exit_stack.enter_async_context(ClientSession(read, write))
            await mcp_session.initialize()
            tool_list, resource_list, prompt_list = await self._get_mcp_action_list(mcp_session)
                
            return tool_list, resource_list, prompt_list, mcp_session
            
        except Exception as e:
            await self._cleanup_mcp()
            raise ValueError(f"Failed to connect to MCP server with SSE: {e}")

    # async def _setup_websocket_mcp(self) -> tuple[list[ToolEntity], list[ToolEntity], list[ToolEntity], ClientSession]:
    #     """Connect to an MCP server running with WebSocket transport"""
    #     raise NotImplementedError

    async def _cleanup_mcp(self):
        """Clean up MCP Client session."""
        if hasattr(self, 'exit_stack') and self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception as e:
                if "Event loop is closed" not in str(e):
                    print(f"Resource release error: {e}")
            finally:
                self.exit_stack = None
                self.mcp_sessions = None

    def _run_async(self, coroutine):
        """Helper function to run async coroutine synchronously (semaphore method)"""
        try:
            # check if there is a running event loop
            try:
                loop = asyncio.get_running_loop()
                # run on existing event loop
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                return future.result(timeout=300)  # 300 seconds is operation timeout
            except RuntimeError:
                # create a new event loop if there is no running loop
                with self._loop_lock:  # lock thread safely
                    # create a shared loop if it does not exist yet
                    if self._shared_loop is None:
                        self._shared_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(self._shared_loop)
                    # increase user count
                    self._loop_users += 1
                
                loop = self._shared_loop
                try:
                    # run coroutine on shared event loop
                    return loop.run_until_complete(coroutine)

                finally:
                    with self._loop_lock:  # lock thread safely
                        # decrease user count
                        self._loop_users -= 1

        except Exception as e:
            print(f"error in async execution: {str(e)}")
            return f"error in async execution: {str(e)}"

    def _map_json_schema_type_to_tool_parameter_type(self, json_type: str) -> str:
        """Convert JSON Schema type to Dify ToolParameter type"""
        type_mapping = {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "object": "string",
            "array": "string",
        }
        return type_mapping.get(json_type, "string")

    def _convert_mcp_tool_to_tool_entity(self, mcp_tool: Tool, mcp_server_name: str) -> ToolEntity:
        """Convert MCP Tool to Dify ToolEntity"""
        
        # add processing for tuples
        if isinstance(mcp_tool, tuple):
            # print(f"DEBUG: Tool is a tuple with {len(mcp_tool)} elements: {mcp_tool}")
            
            ### MCP SDK ###
            # class Tool(BaseModel):
            #     """Internal tool registration info."""

            #     fn: Callable = Field(exclude=True)
            #     name: str = Field(description="Name of the tool")
            #     description: str = Field(description="Description of what the tool does")
            #     parameters: dict = Field(description="JSON schema for tool parameters")
            #     fn_metadata: FuncMetadata = Field(
            #         description="Metadata about the function including a pydantic model for tool"
            #         " arguments"
            #     )
            #     is_async: bool = Field(description="Whether the tool is async")
            #     context_kwarg: str | None = Field(
            #         None, description="Name of the kwarg that should receive context"
            #     )

            # if name, description, inputSchema are elements of the tuple
            if len(mcp_tool) >= 3:
                print(f"DEBUG: Extracting tool information from tuple")
                tool_name = mcp_tool[0]
                tool_description = mcp_tool[1]
                tool_input_schema = mcp_tool[2]
            else:
                print(f"DEBUG: Not enough elements in the tuple, using dummy data")
                # use dummy data if the necessary information is missing
                tool_name = f"unknown_tool_{id(mcp_tool)}"
                tool_description = "Unknown tool description"
                tool_input_schema = {}
        else:
            # if it is the expected object type
            tool_name = getattr(mcp_tool, "name", f"unknown_tool_{id(mcp_tool)}")
            tool_description = getattr(mcp_tool, "description", "")
            tool_input_schema = getattr(mcp_tool, "inputSchema", {})
            # print(f"DEBUG: Tool name: {tool_name}")
            # print(f"DEBUG: Tool description: {tool_description}")
            # print(f"DEBUG: Tool input schema: {tool_input_schema}")
        
        identity = AgentToolIdentity(
            author="MCP",
            name=f"{mcp_server_name}_mcp_tool_{tool_name}",
            label=I18nObject(
                en_US=tool_name,
            ),
            provider="mcp"
        )
        
        description = ToolDescription(
            human=I18nObject(
                en_US=tool_description or f"MCP Tool: {tool_name}"
            ),
            llm=tool_description or f"Tool provided by MCP server: {tool_name}"
        )
        
        parameters = []
        if tool_input_schema and isinstance(tool_input_schema, dict) and "properties" in tool_input_schema:
            for param_name, param_info in tool_input_schema["properties"].items():
                required = param_name in (tool_input_schema.get("required", []) or [])
                param_type = self._map_json_schema_type_to_tool_parameter_type(param_info.get("type", "string"))
                
                # tool_input_schema example:
                # {'type': 'object',
                #   'properties': 
                #   {   
                #     'query': {'type': 'string'}, 
                #     'max_results': {'type': 'integer'}, 
                #     'date_from': {'type': 'string'}, 
                #     'date_to': {'type': 'string'},
                #     'categories': { 'type': 'array', 
                #                     'items': {'type': 'string'} }
                #   }, 
                #   'required': ['query']
                # }
                
                parameter = ToolParameter(
                    name=param_name, # The name of the parameter
                    label=I18nObject(en_US=param_name), # The label presented to the user
                    human_description= description.human,# The description presented to the user
                    type=param_type,
                    required=required,
                    form=ToolParameter.ToolParameterForm.LLM,
                    llm_description=description.llm
                )
                parameters.append(parameter)
        
        return ToolEntity(
            identity=identity,
            description=description,
            parameters=parameters,
            provider_type=ToolProviderType.API,
            has_runtime_parameters=True
        )

    def _convert_mcp_resource_to_tool_entity(self, mcp_resource: Resource, mcp_server_name: str) -> ToolEntity:
        """Convert MCP Resource to Dify ToolEntity"""
        
        # add processing for tuples
        if isinstance(mcp_resource, tuple):
            print(f"DEBUG: Resource is a tuple with {len(mcp_resource)} elements: {mcp_resource}")
            if len(mcp_resource) >= 4:
                resource_name = mcp_resource[0]
                resource_description = mcp_resource[1]
                resource_mimeType = mcp_resource[2]
                resource_uri = mcp_resource[3]
            else:
                resource_name = f"unknown_resource_{id(mcp_resource)}"
                resource_description = "Unknown resource description"
                resource_mimeType = ""
                resource_uri = ""
        else:
            resource_name = getattr(mcp_resource, "name", f"unknown_resource_{id(mcp_resource)}")
            resource_description = getattr(mcp_resource, "description", "")
            resource_mimeType = getattr(mcp_resource, "mimeType", "")
            resource_uri = getattr(mcp_resource, "uri", "")
        
        identity = AgentToolIdentity(
            author="MCP",
            name=f"{mcp_server_name}_mcp_resource_{resource_name}",
            label=I18nObject(
                en_US=f"Read {resource_name}"
            ),
            provider="mcp"
        )
        
        description = ToolDescription(
            human=I18nObject(
                en_US=f"Read resource: {resource_description or resource_name}"
            ),
            llm=f"Read the resource '{resource_name}' from MCP server. " + 
                (f"Description: {resource_description}" if resource_description else "") +
                (f" (MIME type: {resource_mimeType})" if resource_mimeType else "")
        )
        
        # fixed parameters - the URI of the resource is automatically set and not needed
        parameters = []
        
        return ToolEntity(
            identity=identity,
            description=description,
            parameters=parameters,
            provider_type=ToolProviderType.API,
            has_runtime_parameters=True,
            runtime_parameters={"uri": str(resource_uri)} 
        )

    def _convert_mcp_prompt_to_tool_entity(self, mcp_prompt: Prompt, mcp_server_name: str) -> ToolEntity:
        """Convert MCP Prompt to Dify ToolEntity"""
        
        # add processing for tuples
        if isinstance(mcp_prompt, tuple):
            print(f"DEBUG: Prompt is a tuple with {len(mcp_prompt)} elements: {mcp_prompt}")
            if len(mcp_prompt) >= 3:
                prompt_name = mcp_prompt[0]
                prompt_description = mcp_prompt[1]
                prompt_arguments = mcp_prompt[2]
            else:
                prompt_name = f"unknown_prompt_{id(mcp_prompt)}"
                prompt_description = "Unknown prompt description"
                prompt_arguments = []
        else:
            prompt_name = getattr(mcp_prompt, "name", f"unknown_prompt_{id(mcp_prompt)}")
            prompt_description = getattr(mcp_prompt, "description", "")
            prompt_arguments = getattr(mcp_prompt, "arguments", [])
        
        identity = AgentToolIdentity(
            author="MCP",
            name=f"{mcp_server_name}_mcp_prompt_{prompt_name}",
            label=I18nObject(
                en_US=f"Use prompt: {prompt_name}"
            ),
            provider="mcp"
        )
        
        description = ToolDescription(
            human=I18nObject(
                en_US=f"Use prompt template: {prompt_description or prompt_name}"
            ),
            llm=f"Use the prompt template '{prompt_name}' from MCP server. " + 
                (f"Description: {prompt_description}" if prompt_description else "")
        )
        
        # convert arguments to parameters
        parameters = []
        if prompt_arguments:
            for arg in prompt_arguments:
                parameter = ToolParameter(
                    name=arg.name,
                    label=I18nObject(en_US=arg.name),
                    type="string",  # string prompt arguments are string by default
                    required=arg.required or False,
                    form=ToolParameter.ToolParameterForm.LLM,
                    llm_description=arg.description or f"Argument for prompt: {arg.name}",
                    human_description=I18nObject(en_US=arg.description or f"Prompt argument: {arg.name}"),
                )
                parameters.append(parameter)
        
        return ToolEntity(
            identity=identity,
            description=description,
            parameters=parameters,
            provider_type=ToolProviderType.API,
            has_runtime_parameters=True,
            runtime_parameters={"prompt_name": prompt_name},
        )

    def _convert_mcp_components_to_tool_entities(
            self,
            mcp_server_name: str,
            mcp_tool_list: ListToolsResult,
            mcp_resource_list: ListResourcesResult,
            mcp_prompt_list: ListPromptsResult
        ) -> list[ToolEntity]:
        """Convert MCP components to Dify ToolEntity list"""
        tool_entities = []
        
        # extract and convert tools
        actual_tools = []
        for item in mcp_tool_list:
            if isinstance(item, tuple) and len(item) == 2 and item[0] == 'tools':
                # extract the actual tool list from the tuple with the 'tools' key
                actual_tools = item[1]
                break
        
        for tool in actual_tools:
            tool_entities.append(self._convert_mcp_tool_to_tool_entity(tool, mcp_server_name))
        
        # process resources similarly
        actual_resources = []
        for item in mcp_resource_list:
            if isinstance(item, tuple) and len(item) == 2 and item[0] == 'resources':
                actual_resources = item[1]
                break
        
        for resource in actual_resources:
            tool_entities.append(self._convert_mcp_resource_to_tool_entity(resource, mcp_server_name))

        # process prompts similarly
        actual_prompts = []
        for item in mcp_prompt_list:
            if isinstance(item, tuple) and len(item) == 2 and item[0] == 'prompts':
                actual_prompts = item[1]
                break
        
        for prompt in actual_prompts:
            tool_entities.append(self._convert_mcp_prompt_to_tool_entity(prompt, mcp_server_name))
        
        return tool_entities
