import json
import time
import argparse
import traceback
import hashlib
from copy import deepcopy
#from lib import name_utils, llm_utils
from lib.repo_interface import get_repo_interface
import ollama
from langchain_experimental.llms.ollama_functions import OllamaFunctions
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, FunctionMessage
from langchain.load.dump import dumps

RESULT_DIR = './results/'

#INSERTED>>
def format_message_for_llm(message_data):
    """
    Formats a list of dictionaries containing message information into a
    Python list format that is more readable and suitable for LLM processing.
    
    Args:
    message_data (list of dict): The message data to be formatted.
    
    Returns:
    str: A formatted string representation of the messages suitable for LLM.
    """
    formatted_messages = []
    for message in message_data:
        the_role = message.get('role')
        if the_role != 'system' and the_role != 'user' and the_role != 'assistant':
            print(the_role)
            the_role = 'system'
        formatted_message = {
            'role': the_role
        }
        
        if 'content' in message:
            formatted_message['content'] = f"""{message['content']}"""
        
        if 'function_call' in message:
            func_call = message['function_call']
            formatted_message['function_call'] = {
                'name': func_call['name'],
                'arguments': func_call['arguments']
            }

        if 'name' in message:
            formatted_message['name'] = message['name']

        formatted_messages.append(formatted_message)

    return formatted_messages
#INSERTED<<

class AutoDebugger():
    def __init__(self, bug_name, model_type, system_file, test_offset=None,
            max_num_tests=None, allow_multi_predictions=False,
            summarize_messages=False, debug=False, **ri_kwargs):
        super().__init__()
        self._bug_name = bug_name
        self._model = model_type
        self._ri = get_repo_interface(bug_name, **ri_kwargs)
        self._test_offset = test_offset
        self._max_num_tests = max_num_tests
        self._allow_multi_predictions = allow_multi_predictions
        self._summarize_messages = summarize_messages
        self._system_file = system_file
        self._debug = debug
        self.model = OllamaFunctions(model="codellama", format="json")

    def _replace_last_with_memo(self, memo):
        self.messages = self.messages[:-1] # replace recent two queries with memo
        self.messages.append(AIMessage(conten='Summary: ' + memo))

    def _append_to_messages(self, message):
        # to easily control debug behavior
        if self._debug:
            print(message)
        self.messages.append(message)

    @property
    def _system_message(self):
        with open(self._system_file) as f:
            system_message = f.read().strip()
        function_descriptions = "\n".join(
            [f"Function name: {func['name']}\nDescription: {func['description']}\nParameters: {json.dumps(func['parameters'], indent=4)}\n"
             for func in self._ri.function_descriptions]
        )
        system_message += f"\n\nAvailable functions:\n{function_descriptions}"
        system_message += "\n\nUse these functions to retrieve necessary information. Respond in JSON format with function calls."
        return system_message

    def _init_interaction_records(self):
        self._mid_map = {} # md5_hash -> mid (message id)
        self._message_map = {} # mid -> message
        self._interaction_records = [] # list of dict

    def _append_to_interaction_records(self, prompt_messages, response_message):
        def _save_message_and_get_mid(message):
            s = dumps(message).encode('utf-8')
            md5_hash = hashlib.md5(s).digest()
            if md5_hash not in self._mid_map:
                self._mid_map[md5_hash] = f"m{len(self._mid_map) + 1}"
                self._message_map[self._mid_map[md5_hash]] = deepcopy(message)
            return self._mid_map[md5_hash]

        self._interaction_records.append({
            "prompt_messages": [_save_message_and_get_mid(m) for m in prompt_messages],
            "response_message": _save_message_and_get_mid(response_message)
        })

    def startup(self):
        self._init_interaction_records()
        self.messages = []

        tools =[{
            "name": func['name'], 
            "description": func['description'],
            "parameters": func['parameters']
            }
             for func in self._ri.function_descriptions]
        self.model = self.model.bind_tools(tools=tools, tool_choice='any')

        self._append_to_messages(SystemMessage(content=self._system_message))

        fail_test_signatures = [
            signature for signature in self._ri.failing_test_signatures
            if self._ri.get_test_snippet(signature) is not None
        ]

        if self._test_offset is not None:
            # rotate list
            offset = self._test_offset % len(fail_test_signatures)
            fail_test_signatures = fail_test_signatures[offset:] + fail_test_signatures[:offset]

        if self._max_num_tests is not None:
            fail_test_signatures = fail_test_signatures[:self._max_num_tests]

        if not fail_test_signatures:
            raise ValueError(f'Could not find test snippet for bug {self._bug_name}')

        user_message = f"The test `{fail_test_signatures}` failed.\n"
        test_snippets = "\n\n".join(self._ri.get_test_snippet(signature).rstrip() for signature in fail_test_signatures)
        user_message += f"The test looks like:\n\n```{self._ri.language}\n{test_snippets}\n```\n\n"

        failing_traces = "\n\n".join(self._ri.get_fail_info(signature, minimize=True).rstrip() for signature in fail_test_signatures)
        user_message += f"It failed with the following error message and call stack:\n\n```\n{failing_traces}\n```\n\n"

        user_message += f'Start by calling the `{self._ri.initial_coverage_getter}` function.'

        self._append_to_messages(HumanMessage(content=user_message))

        # no-LLM call of first instruction (LLM always calls this anyway)
        self._append_to_messages(AIMessage(
            content="",
            additional_kwargs={
                "function_call": {
                    "name": "get_covered_packages",
                    "arguments": "{}"
                }
            }
        ))
        self._append_to_messages(SystemMessage(
            name=self._ri.initial_coverage_getter,
            content=json.dumps(self._ri.fname2func[self._ri.initial_coverage_getter]())
        ))

    def call_function(self, response_message):
        function_name = response_message["function_call"]["name"]
        function_to_call = self._ri.fname2func[function_name]
        if response_message["function_call"]["arguments"]:
            function_args = json.loads(response_message["function_call"]["arguments"])
        else:
            function_args = {}
        function_response = function_to_call(**function_args)
        return function_name, function_response

    def step(self, function_call_mode="auto"):
        if self._summarize_messages:
            prompt_messages = self.messages + [SystemMessage(content='Summarize the important content of the immediate prior message. If you are unsure of the solution, call a function afterwards. Be concise, but fully qualify all names.')]
        else:
            prompt_messages = self.messages

        #INSERTED>>

        """
        the original response used by auto FL
        """
        # response = self.get_LLM_response(
        #     model=self._model,
        #     messages=prompt_messages,
        #     functions=self._ri.function_descriptions,
        #     function_call=function_call_mode,  # auto is default, but we'll be explicit #FIXME
        # )

        
        '''
        example message format for ollama

        role = either system, user, or assistant
        '''
        

        print("start")
#         response = ollama.chat(model='codellama', messages=[
#   {
#     'role': 'system',
#     'content': """
# You are a debugging assistant. You will be presented with a failing test, and tools (functions) to access the source code of the system under test (SUT). Your task is to provide a step-by-step explanation of how the bug occurred, based on the failing test and the information you retrieved using tests about the SUT. You will be given 9 chances to interact with functions to gather relevant information. An example answer would look like follows.\n\nTitle: Diagnosis of test `testGenerateURLFragment`\nDetails: The test `testGenerateURLFragment` checks whether HTML entities such as `&quot;` are correctly escaped to their corresponding character by the `StandardToolTipTagFragmentGenerator.generateToolTipFragment` method. However, the test failure indicates that this is not the case. Following the execution path, we find that `StandardToolTipTagFragmentGenerator.generateToolTipFragment` is not escaping any HTML code; instead, it is using its input `toolTipText` as is. Consequently, un-escaped strings are being returned, which leads to the error demonstrated by 
# the test.\n\nAfter providing this diagnosis, you will be prompted to suggest which methods would be the best locations to be fixed. The answers should be in the form of `ClassName.MethodName(ArgType1, ArgType2, ...)` without commentary (one per line), as your answer will be automatically processed before finally being presented to the user.
# """
#   },
#   {
#       'role': 'user',
#       'content': """
# The test `[\'com.google.javascript.jscomp.ClosureCodingConventionTest.testRequire()\']` failed.\nThe test looks like:\n\n```java\n194 :   public void testRequire() {\n196 :     assertNotRequire("goog.require(foo)"); // error occurred here\n199 :   }\n```\n\nIt failed with the following error message and call stack:\n\n```\njunit.framework.AssertionFailedError: Expected: <null> but was: foo\n\tat com.google.javascript.jscomp.ClosureCodingConventionTest.assertNotRequire(ClosureCodingConventionTest.java:218)\n\tat com.google.javascript.jscomp.ClosureCodingConventionTest.testRequire(ClosureCodingConventionTest.java:196)\n```\n\nStart by calling the `get_failing_tests_covered_classes` function.
# """
#   },
#   {
#       'role': 'assistant',
#       'content': 'None',
#       'function_call' : """
# 'name': 'get_failing_tests_covered_classes', 'arguments': '{ }'
# """
#   },
#   {
#       'role': 'assistant',
#       'name' : 'get_failing_tests_covered_classes',
#       'content': """
# '{"com.google.javascript.jscomp": ["JSSourceFile", "JsAst", "SyntacticScopeCreator", "BasicErrorManager", "ErrorFormat", "PrepareAst", "ProcessTweaks", "NodeUtil", "Compiler", "Tracer", "CodeChangeHandler", "CheckLevel", "RhinoErrorReporter", "PassFactory", "DiagnosticGroupWarningsGuard", "SourceFile", "AbstractMessageFormatter", "AnonymousFunctionNamingPolicy", "ComposeWarningsGuard", "NodeTraversal", "WarningsGuard", "DiagnosticType", "LoggerErrorManager", "LightweightMessageFormatter", "CompilerInput", "ClosureCodingConvention", "DiagnosticGroups", "CompilerOptions", "SuppressDocWarningsGuard", "DiagnosticGroup"], "com.google.javascript.rhino": ["Context", "InputId", "Node", "ScriptRuntime"],
#  "com.google.javascript.jscomp.parsing": ["IRFactory", "TypeSafeDispatcher", "ParserRunner", "Config"], "com.google.javascript.rhino.jstype": ["ObjectType"]}'
# """
#   }
#  ])
        

        for message in self.messages:
            print(type(message))

        #message = format_message_for_llm(prompt_messages)
        response = self.model.invoke(prompt_messages)

        print(response)

        #INSERTED<<

        if self._summarize_messages:
            llm_summary = response
            if llm_summary is not None:
                self._replace_last_with_memo(llm_summary)

        response_message = json.loads(dumps(response))["kwargs"]

        self._append_to_interaction_records(prompt_messages, response_message)

        # check if GPT wanted to call a function
        if "function_call" in response_message['additional_kwargs']:
            # call the function

            try: # Note: the JSON response may not always be valid; be sure to handle errors
                function_name, function_response = self.call_function(response_message["additional_kwargs"])
            except Exception as e:
                if self._debug or isinstance(e, KeyboardInterrupt):
                    raise e
                else:
                    return False # drop erroneous response and retry if step budget left

            self._append_to_messages(response) # extend conversation with assistant's reply
            # send the info on the function call and function response to GPT
            function_message = SystemMessage(
                name=function_name,
                content=json.dumps(function_response),
            )
            self._append_to_messages(function_message)
            return False # not done
        else:
            self._append_to_messages(response_message)  # extend conversation with assistant's reply
            return True

    def finish(self):
        finishing_string = "Based on the available information, provide the signatures of the most likely culprit methods for the bug. Your answer will be processed automatically, so make sure to only answer with the accurate signatures of all likely culprits (in `ClassName.MethodName(ArgType1, ArgType2, ...)` format), without commentary (one per line). "
        if not self._allow_multi_predictions:
            finishing_string = finishing_string.replace('signatures', 'signature')
            finishing_string = finishing_string.replace('methods', 'method')
            finishing_string = finishing_string.replace(' (one per line)', '')
            finishing_string = finishing_string.replace('all likely culprits', 'the most likely culprit')

        querying_buggy_methods = {
            'role': 'user',
            'content': finishing_string
        }
        self._append_to_messages(querying_buggy_methods)
        response = self.get_LLM_response(
            model=self._model,
            messages=self.messages,
        )
        response_message = response["message"]
        self._append_to_messages(response_message)
        return response_message['content'].strip()

    def grade(self, answer):
        if self._allow_multi_predictions:
            pred_exprs = answer.splitlines()
        else:
            pred_exprs = [answer]

        matching_method_signatures = {
            pred_expr: self._ri.get_matching_method_signatures(pred_expr)
            for pred_expr in pred_exprs
        }

        grade_result = {}
        for method in self._ri.buggy_method_signatures:
            pred_match = [
                pred_expr for pred_expr in pred_exprs
                if method in matching_method_signatures[pred_expr]
            ]
            grade_result[method] = {
                'is_found': len(pred_match) > 0,
                'matching_answer': pred_match
            }
        return grade_result

    def run(self, budget=10):
        self.startup()
        for i in range(budget):
            if i == budget - 1:
                function_call_mode = "none"
            else:
                function_call_mode = "auto"
            done = self.step(function_call_mode)
            time.sleep(0.1)
            if done:
                break
        final_response = self.finish()
        grade_result = self.grade(final_response)
        return grade_result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--model', default='gpt-3.5-turbo-0613')
    parser.add_argument('-b', '--bug_name', default='Chart_1')
    parser.add_argument('-o', '--out', default='test.json')
    parser.add_argument('-p', '--prompt', default='prompts/system_msg_expbug.txt')
    parser.add_argument('-t', '--max_num_tests', default=None, type=int)
    parser.add_argument('--test_offset', default=0, type=int)
    parser.add_argument('--max_budget', default=10, type=int)
    parser.add_argument('--allow_multi_predictions', action="store_true")
    parser.add_argument('--summarize_messages', action="store_true")
    parser.add_argument('--show_line_number', action="store_true")
    parser.add_argument('--postprocess_test_snippet', action="store_true")
    parser.add_argument('--debug', action="store_true")
    args = parser.parse_args()

    ad = AutoDebugger(args.bug_name, args.model, args.prompt,
        test_offset=args.test_offset,
        max_num_tests=args.max_num_tests,
        allow_multi_predictions=args.allow_multi_predictions,
        summarize_messages=args.summarize_messages,
        show_line_number=args.show_line_number,
        postprocess_test_snippet=args.postprocess_test_snippet,
        debug=args.debug
    )

    try:
        grade = ad.run(args.max_budget)
    except Exception as e:
        grade = traceback.format_exc()
        if args.debug:
            raise e
    
    ad.messages = list(map(lambda x: json.loads(dumps(x))["kwargs"], ad.messages))

    with open(args.out, "w") as f:
        json.dump({
            'time': time.time(),
            'messages': ad.messages,
            'interaction_records': {
                "step_histories": ad._interaction_records
            },
            'buggy_methods': grade,
        }, f, indent=4)