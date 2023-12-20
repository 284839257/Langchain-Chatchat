import asyncio
import json
from typing import List, Union, AsyncIterable, Dict

from fastapi import Body
from fastapi.responses import StreamingResponse

from langchain.agents import initialize_agent, AgentType
from langchain_core.output_parsers import StrOutputParser
from langchain.chains import LLMChain
from langchain.prompts.chat import ChatPromptTemplate
from langchain.prompts import PromptTemplate
from langchain_core.runnables import RunnableBranch

from server.agent.agent_factory import initialize_glm3_agent, initialize_qwen_agent
from server.agent.tools_factory.tools_registry import all_tools
from server.agent.container import container

from server.utils import wrap_done, get_ChatOpenAI, get_prompt_template
from server.chat.utils import History
from server.memory.conversation_db_buffer_memory import ConversationBufferDBMemory
from server.db.repository import add_message_to_db
from server.callback_handler.agent_callback_handler import AgentStatus, AgentExecutorAsyncIteratorCallbackHandler


def create_models_from_config(configs, callbacks, stream):
    if configs is None:
        configs = {}
    models = {}
    prompts = {}
    for model_type, model_configs in configs.items():
        for model_name, params in model_configs.items():
            callbacks = callbacks if params.get('callbacks', False) else None
            model_instance = get_ChatOpenAI(
                model_name=model_name,
                temperature=params.get('temperature', 0.5),
                max_tokens=params.get('max_tokens', 1000),
                callbacks=callbacks,
                streaming=stream,
            )
            models[model_type] = model_instance
            prompt_name = params.get('prompt_name', 'default')
            prompt_template = get_prompt_template(type=model_type, name=prompt_name)
            prompts[model_type] = prompt_template
    return models, prompts


# 在这里写构建逻辑
def create_models_chains(history, history_len, prompts, models, tools, callbacks, conversation_id, metadata):
    memory = None
    chat_prompt = None
    container.metadata = metadata

    if history:
        history = [History.from_data(h) for h in history]
        input_msg = History(role="user", content=prompts["llm_model"]).to_msg_template(False)
        chat_prompt = ChatPromptTemplate.from_messages(
            [i.to_msg_template() for i in history] + [input_msg])
    elif conversation_id and history_len > 0:
        memory = ConversationBufferDBMemory(
            conversation_id=conversation_id,
            llm=models["llm_model"],
            message_limit=history_len
        )
    else:
        input_msg = History(role="user", content=prompts["llm_model"]).to_msg_template(False)
        chat_prompt = ChatPromptTemplate.from_messages([input_msg])

    chain = LLMChain(
        prompt=chat_prompt,
        llm=models["llm_model"],
        memory=memory
    )
    classifier_chain = (
            PromptTemplate.from_template(prompts["preprocess_model"])
            | models["preprocess_model"]
            | StrOutputParser()
    )

    if "action_model" in models and tools:
        if "chatglm3" in models["action_model"].model_name.lower():
            agent_executor = initialize_glm3_agent(
                llm=models["action_model"],
                tools=tools,
                prompt=prompts["action_model"],
                memory=memory,
                callbacks=callbacks,
                verbose=True,
            )
        elif "qwen" in models["action_model"].model_name.lower():
            agent_executor = initialize_qwen_agent(
                llm=models["action_model"],
                tools=tools,
                prompt=prompts["action_model"],
                memory=memory,
                callbacks=callbacks,
                verbose=True,
            )
        else:
            agent_executor = initialize_agent(
                llm=models["action_model"],
                tools=tools,
                callbacks=callbacks,
                agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
                memory=memory,
                verbose=True,
            )

        # branch = RunnableBranch(
        #     (lambda x: "1" in x["topic"].lower(), agent_executor),
        #     chain
        # )
        # full_chain = ({"topic": classifier_chain, "input": lambda x: x["input"]} | branch)
        full_chain = ({"input": lambda x: x["input"]} | agent_executor)
    else:
        full_chain = ({"input": lambda x: x["input"]} | chain)
    return full_chain


async def chat(query: str = Body(..., description="用户输入", examples=["恼羞成怒"]),
               metadata: dict = Body({}, description="附件，可能是图像或者其他功能", examples=[]),
               conversation_id: str = Body("", description="对话框ID"),
               history_len: int = Body(-1, description="从数据库中取历史消息的数量"),
               history: Union[int, List[History]] = Body(
                   [],
                   description="历史对话，设为一个整数可以从数据库中读取历史消息",
                   examples=[
                       [
                           {"role": "user",
                            "content": "我们来玩成语接龙，我先来，生龙活虎"},
                           {"role": "assistant", "content": "虎头虎脑"}
                       ]
                   ]
               ),
               stream: bool = Body(True, description="流式输出"),
               model_config: Dict = Body({}, description="LLM 模型配置"),
               tool_config: Dict = Body({}, description="工具配置"),
               ):
    async def chat_iterator() -> AsyncIterable[str]:
        message_id = add_message_to_db(
            chat_type="llm_chat",
            query=query,
            conversation_id=conversation_id
        ) if conversation_id else None

        callback = AgentExecutorAsyncIteratorCallbackHandler()
        callbacks = [callback]

        # 从配置中选择模型
        models, prompts = create_models_from_config(callbacks=[], configs=model_config, stream=stream)

        # 从配置中选择工具
        tools = [tool for tool in all_tools if tool.name in tool_config]

        # 构建完整的Chain
        full_chain = create_models_chains(prompts=prompts,
                                          models=models,
                                          conversation_id=conversation_id,
                                          tools=tools,
                                          callbacks=callbacks,
                                          history=history,
                                          history_len=history_len,
                                          metadata=metadata)

        # Execute Chain

        task = asyncio.create_task(
            wrap_done(full_chain.ainvoke({"input": query}), callback.done))

        async for chunk in callback.aiter():
            data = json.loads(chunk)
            data["message_id"] = message_id
            yield json.dumps(data, ensure_ascii=False)

        await task

    return StreamingResponse(chat_iterator(), media_type="text/event-stream")
