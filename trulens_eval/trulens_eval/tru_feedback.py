"""
# Feedback Functions

Initialize feedback function providers:

```python
    hugs = Huggingface()
    openai = OpenAI()
```

Run feedback functions. See examples below on how to create them:

```python
    feedbacks = tru.run_feedback_functions(
        chain=chain,
        record=record,
        feedback_functions=[f_lang_match, f_qs_relevance]
    )
```

## Examples:

Non-toxicity of response:

```python
    f_non_toxic = Feedback(hugs.not_toxic).on_response()
```

Language match feedback function:

```python
    f_lang_match = Feedback(hugs.language_match).on(text1="prompt", text2="response")
```

"""

from datetime import datetime
from inspect import Signature
from inspect import signature
import logging
from multiprocessing.pool import AsyncResult
import re
from typing import (Any, Callable, Dict, List, Optional, Sequence, Tuple, Type,
                    Union)

import numpy as np
import openai
import pydantic
from tqdm.auto import tqdm

from trulens_eval import feedback_prompts
from trulens_eval.keys import *
from trulens_eval.provider_apis import Endpoint
from trulens_eval.tru_db import JSON, ChainQuery, RecordInput, RecordOutput, RecordQuery
from trulens_eval.tru_db import obj_id_of_obj
from trulens_eval.tru_db import Query
from trulens_eval.tru_db import Record
from trulens_eval.tru_db import TruDB
from trulens_eval.schema import FeedbackDefinition, FeedbackResult, Method, Model, Selection
from trulens_eval.util import TP, JSONPath

# openai

# (external) feedback-
# provider
# model

# feedback_collator:
# - record, feedback_imp, selector -> dict (input, output, other)

# (external) feedback:
# - *args, **kwargs -> real
# - dict -> real
# - (record, selectors) -> real
# - str, List[str] -> real
#    agg(relevance(str, str[0]),
#        relevance(str, str[1])
#    ...)

# (internal) feedback_imp:
# - Option 1 : input, output -> real
# - Option 2: dict (input, output, other) -> real


# "prompt" or "input" mean overall chain input text
# "response" or "output"mean overall chain output text
# Otherwise a Query is a path into a record structure.

PROVIDER_CLASS_NAMES = ['OpenAI', 'Huggingface', 'Cohere']


def check_provider(cls_or_name: Union[Type, str]) -> None:
    if isinstance(cls_or_name, str):
        cls_name = cls_or_name
    else:
        cls_name = cls_or_name.__name__

    assert cls_name in PROVIDER_CLASS_NAMES, f"Unsupported provider class {cls_name}"



class Feedback(FeedbackDefinition):
    # Implementation, not serializable, note that FeedbackDefinition contains
    # `implementation` mean to serialize the below.
    imp: Optional[Callable] = pydantic.Field(exclude=True)
    
    def __init__(
        self,
        imp: Optional[Callable] = None,
        *args, **kwargs
    ):
        """
        A Feedback function container.

        Parameters:
        
        - imp: Optional[Callable] -- implementation of the feedback function.
        """

        if imp is not None:
            # These are for serialization to/from json and for db storage.
            kwargs['implementation'] = Method.of_method(imp)

        super().__init__(*args, **kwargs)

        self.imp = imp

        # Verify that `imp` expects the arguments specified in `selectors`:
        if self.imp is not None and self.selectors is not None:
            sig: Signature = signature(self.imp)
            for argname in self.selectors.keys():
                assert argname in sig.parameters, (
                    f"{argname} is not an argument to {self.imp.__name__}. "
                    f"Its arguments are {list(sig.parameters.keys())}."
                )

    @staticmethod
    def evaluate_deferred(tru: 'Tru'):
        db = tru.db

        def prepare_feedback(row):
            record_json = row.record_json

            feedback = Feedback.of_json(row.feedback_json)
            feedback.run_and_log(record_json=record_json, tru=tru)

        feedbacks = db.get_feedback()

        for i, row in feedbacks.iterrows():
            if row.status == 0:
                tqdm.write(f"Starting run for row {i}.")

                TP().runlater(prepare_feedback, row)
            elif row.status in [1]:
                now = datetime.now().timestamp()
                if now - row.last_ts > 30:
                    tqdm.write(f"Incomplete row {i} last made progress over 30 seconds ago. Retrying.")
                    TP().runlater(prepare_feedback, row)
                else:
                    tqdm.write(f"Incomplete row {i} last made progress less than 30 seconds ago. Giving it more time.")

            elif row.status in [-1]:
                now = datetime.now().timestamp()
                if now - row.last_ts > 60*5:
                    tqdm.write(f"Failed row {i} last made progress over 5 minutes ago. Retrying.")
                    TP().runlater(prepare_feedback, row)
                else:
                    tqdm.write(f"Failed row {i} last made progress less than 5 minutes ago. Not touching it for now.")

            elif row.status == 2:
                pass

        # TP().finish()
        # TP().runrepeatedly(runner)

    #@property
    #def json(self):
    #    assert hasattr(self, "_json"), "Cannot json-size partially defined feedback function."
    #    return self._json

    """
    @property
    def feedback_id(self):
        assert hasattr(self, "_feedback_id"), "Cannot get id of partially defined feedback function."
        return self._feedback_id
    """

    def __call__(self, *args, **kwargs) -> Any:
        assert self.imp is not None, "Feedback definition needs an implementation to call."
        return self.imp(*args, **kwargs)
        
    @staticmethod
    def selection_to_json(select: Selection) -> dict:
        if isinstance(select, str):
            return select
        elif isinstance(select, Query):
            return select._path
        else:
            raise ValueError(f"Unknown selection type {type(select)}.")

    @staticmethod
    def selection_of_json(obj: Union[List, str]) -> Selection:
        if isinstance(obj, str):
            return obj
        elif isinstance(obj, (List, Tuple)):
            return JSONPath.of_path(obj)  # TODO
        else:
            raise ValueError(f"Unknown selection encoding of type {type(obj)}.")

    """
    def to_json(self) -> dict:
        selectors_json = {
            k: Feedback.selection_to_json(v) for k, v in self.selectors.items()
        }
        return {
            'selectors': selectors_json,
            'imp_json': self.imp_json,
            'feedback_id': self.feedback_id
        }
    """

    @staticmethod
    def of_json(obj) -> 'Feedback':
        assert 'imp_json' in obj,  "Feedback encoding has no 'imp_json' field."
        assert "selectors" in obj, "Feedback encoding has no 'selectors' field."
        
        jobj = obj['imp_json']
        imp_method_name = jobj['method_name']

        selectors = {
            k: Feedback.selection_of_json(v)
            for k, v in obj['selectors'].items()
        }
        provider = Provider.of_json(jobj['provider_class'])

        assert hasattr(
            provider, imp_method_name
        ), f"Provider {provider.__name__} has no feedback function {imp_method_name}."
        imp = getattr(provider, imp_method_name)

        return Feedback(imp, selectors=selectors)

    def on_multiple(
        self,
        multiarg: str,
        each_query: Optional[Query] = None,
        agg: Callable = np.mean
    ) -> 'Feedback':
        """
        Create a variant of `self` whose implementation will accept multiple
        values for argument `multiarg`, aggregating feedback results for each.
        Optionally each input element is further projected with `each_query`.

        Parameters:

        - multiarg: str -- implementation argument that expects multiple values.
        - each_query: Optional[Query] -- a query providing the path from each
          input to `multiarg` to some inner value which will be sent to `self.imp`.
        """

        sig = signature(self.imp)

        def wrapped_imp(*args, **kwargs):
            bindings = sig.bind(*args, **kwargs)

            assert multiarg in bindings.arguments, f"Feedback function expected {multiarg} keyword argument."

            multi = bindings.arguments[multiarg]

            assert isinstance(
                multi, Sequence
            ), f"Feedback function expected a sequence on {multiarg} argument."

            rets: List[AsyncResult[float]] = []

            for aval in multi:

                if each_query is not None:
                    aval = TruDB.project(query=each_query, record_json=aval, chain_json=None)

                bindings.arguments[multiarg] = aval

                rets.append(TP().promise(self.imp, *bindings.args, **bindings.kwargs))

            rets: List[float] = list(map(lambda r: r.get(), rets))

            rets = np.array(rets)

            return agg(rets)

        wrapped_imp.__name__ = self.imp.__name__

        wrapped_imp.__self__ = self.imp.__self__ # needed for serialization

        # Copy over signature from wrapped function. Otherwise signature of the
        # wrapped method will include just kwargs which is insufficient for
        # verify arguments (see Feedback.__init__).
        wrapped_imp.__signature__ = signature(self.imp)

        return Feedback(imp=wrapped_imp, selectors=self.selectors)

    def on_prompt(self, arg: str = "text"):
        """
        Create a variant of `self` that will take in the main chain input or
        "prompt" as input, sending it as an argument `arg` to implementation.
        """

        return Feedback(imp=self.imp, selectors={arg: RecordInput})

    on_input = on_prompt

    def on_response(self, arg: str = "text"):
        """
        Create a variant of `self` that will take in the main chain output or
        "response" as input, sending it as an argument `arg` to implementation.
        """

        return Feedback(imp=self.imp, selectors={arg: RecordOutput})

    on_output = on_response

    def on(self, **selectors):
        """
        Create a variant of `self` with the same implementation but the given `selectors`.
        """

        return Feedback(imp=self.imp, selectors=selectors)

    def run(self, chain: Optional[Model]=None, record: Optional[Record]=None) -> Any:
        """
        Run the feedback function on the given `record`. The `chain` that
        produced the record is also required to determine input/output argument
        names.
        """

        try:
            ins = self.extract_selection(chain=chain, record=record)
            ret = self.imp(**ins)
            
            return FeedbackResult(
                results_json={
                    '_success': True,
                    self.name: ret
                },
                feedback_definition_id = self.feedback_definition_id,
                record_id = record.record_id,
                chain_id=chain.chain_id
            )
        
        except Exception as e:
            return FeedbackResult(
                results_json={
                    '_success': False,
                    '_error': str(e)
                },
                feedback_definition_id = self.feedback_definition_id,
                record_id = record.record_id,
                chain_id=chain.chain_id
            )

    def run_and_log(self, record_json: JSON, tru: 'Tru') -> None:
        record_id = record_json['record_id']
        chain_id = record_json['chain_id']
        
        ts_now = datetime.now().timestamp()

        db = tru.db

        try:
            db.insert_feedback(
                record_id=record_id,
                feedback_id=self.feedback_id,
                last_ts = ts_now,
                status = 1 # in progress
            )

            chain_json = db.get_chain(chain_id=chain_id)

            res = self.run_on_record(chain_json=chain_json, record_json=record_json)

        except Exception as e:
            print(e)
            res = {
                '_success': False,
                'feedback_id': self.feedback_id,
                'record_id': record_json['record_id'],
                '_error': str(e)
            }

        ts_now = datetime.now().timestamp()

        if res['_success']:
            db.insert_feedback(
                record_id=record_id,
                feedback_id=self.feedback_id,
                last_ts = ts_now,
                status = 2, # done and good
                result_json=res,
                total_cost=-1.0, # todo
                total_tokens=-1  # todo
            )
        else:
            # TODO: indicate failure better
            db.insert_feedback(
                record_id=record_id,
                feedback_id=self.feedback_id,
                last_ts = ts_now,
                status = -1, # failure
                result_json=res,
                total_cost=-1.0, # todo
                total_tokens=-1  # todo
            )

    @property
    def name(self):
        """
        Name of the feedback function. Presently derived from the name of the
        function implementing it.
        """

        return self.imp.__name__

    def extract_selection(
            self,
            chain: Model,
            record: Record
        ) -> Dict[str, Any]:
        """
        Given the `chain` that produced the given `record`, extract from
        `record` the values that will be sent as arguments to the implementation
        as specified by `self.selectors`.
        """

        ret = {}

        for k, v in self.selectors.items():
            if isinstance(v, Query):
                q = v

            else:
                raise RuntimeError(f"Unhandled selection type {type(v)}.")

            if q.path[0] == RecordQuery.path[0]:
                o = record.layout_calls_as_chain()
            elif q.path[0] == ChainQuery.path[0]:
                o = chain
            else:
                raise ValueError(f"Query {q} does not indicate whether it is about a record or about a chain.")

            q_within_o = JSONPath(path=q.path[1:])

            val = list(q_within_o(o))

            if len(val) == 1:
                val = val[0]

            ret[k] = val

        return ret


pat_1_10 = re.compile(r"\s*([1-9][0-9]*)\s*")


def _re_1_10_rating(str_val):
    matches = pat_1_10.fullmatch(str_val)
    if not matches:
        # Try soft match
        matches = re.search('[1-9][0-9]*', str_val)
        if not matches:
            logging.warn(f"1-10 rating regex failed to match on: '{str_val}'")
            return -10  # so this will be reported as -1 after division by 10

    return int(matches.group())


class Provider(pydantic.BaseModel):
    endpoint: Any = pydantic.Field(exclude=True)

    @staticmethod
    def of_json(obj: Dict) -> 'Provider':
        cls_name = obj['class_name']
        mod_name = obj['module_name'] # ignored for now
        check_provider(cls_name)

        cls = eval(cls_name)
        kwargs = {k: v for k, v in obj.items() if k not in ['class_name', 'module_name']}

        return cls(**kwargs)

    def to_json(self: 'Provider', **extras) -> Dict:
        obj = {
            'class_name': self.__class__.__name__,
            'module_name': self.__class__.__module__
            }
        obj.update(**extras)
        return obj

class OpenAI(Provider):
    model_engine: str = "gpt-3.5-turbo"

    def __init__(self, model_engine: str = "gpt-3.5-turbo"):
        """
        A set of OpenAI Feedback Functions.

        Parameters:

        - model_engine (str, optional): The specific model version. Defaults to
          "gpt-3.5-turbo".
        """
        super().__init__() # need to include pydantic.BaseModel.__init__

        self.model_engine = model_engine
        self.endpoint = Endpoint(name="openai")

    def to_json(self) -> Dict:
        return Provider.to_json(self, model_engine=self.model_engine)

    def _moderation(self, text: str):
        return self.endpoint.run_me(
            lambda: openai.Moderation.create(input=text)
        )

    def moderation_not_hate(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is hate
        speech.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "hate" and 1 being "not
            hate".
        """
        openai_response = self._moderation(text)
        return 1 - float(
            openai_response["results"][0]["category_scores"]["hate"]
        )

    def moderation_not_hatethreatening(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is
        threatening speech.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "threatening" and 1 being
            "not threatening".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["hate/threatening"]
        )

    def moderation_not_selfharm(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is about
        self harm.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "self harm" and 1 being "not
            self harm".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["self-harm"]
        )

    def moderation_not_sexual(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is sexual
        speech.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "sexual" and 1 being "not
            sexual".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["sexual"]
        )

    def moderation_not_sexualminors(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is about
        sexual minors.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "sexual minors" and 1 being
            "not sexual minors".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["sexual/minors"]
        )

    def moderation_not_violence(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is about
        violence.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "violence" and 1 being "not
            violence".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["violence"]
        )

    def moderation_not_violencegraphic(self, text: str) -> float:
        """
        Uses OpenAI's Moderation API. A function that checks if text is about
        graphic violence.

        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "graphic violence" and 1
            being "not graphic violence".
        """
        openai_response = self._moderation(text)

        return 1 - int(
            openai_response["results"][0]["category_scores"]["violence/graphic"]
        )

    def qs_relevance(self, question: str, statement: str) -> float:
        """
        Uses OpenAI's Chat Completion Model. A function that completes a
        template to check the relevance of the statement to the question.

        Parameters:
            question (str): A question being asked. statement (str): A statement
            to the question.

        Returns:
            float: A value between 0 and 1. 0 being "not relevant" and 1 being
            "relevant".
        """
        return _re_1_10_rating(
            self.endpoint.run_me(
                lambda: openai.ChatCompletion.create(
                    model=self.model_engine,
                    temperature=0.0,
                    messages=[
                        {
                            "role":
                                "system",
                            "content":
                                str.format(
                                    feedback_prompts.QS_RELEVANCE,
                                    question=question,
                                    statement=statement
                                )
                        }
                    ]
                )["choices"][0]["message"]["content"]
            )
        ) / 10

    def relevance(self, prompt: str, response: str) -> float:
        """
        Uses OpenAI's Chat Completion Model. A function that completes a
        template to check the relevance of the response to a prompt.

        Parameters:
            prompt (str): A text prompt to an agent. response (str): The agent's
            response to the prompt.

        Returns:
            float: A value between 0 and 1. 0 being "not relevant" and 1 being
            "relevant".
        """
        return _re_1_10_rating(
            self.endpoint.run_me(
                lambda: openai.ChatCompletion.create(
                    model=self.model_engine,
                    temperature=0.0,
                    messages=[
                        {
                            "role":
                                "system",
                            "content":
                                str.format(
                                    feedback_prompts.PR_RELEVANCE,
                                    prompt=prompt,
                                    response=response
                                )
                        }
                    ]
                )["choices"][0]["message"]["content"]
            )
        ) / 10

    def model_agreement(self, prompt: str, response: str) -> float:
        """
        Uses OpenAI's Chat GPT Model. A function that gives Chat GPT the same
        prompt and gets a response, encouraging truthfulness. A second template
        is given to Chat GPT with a prompt that the original response is
        correct, and measures whether previous Chat GPT's response is similar.

        Parameters:
            prompt (str): A text prompt to an agent. response (str): The agent's
            response to the prompt.

        Returns:
            float: A value between 0 and 1. 0 being "not in agreement" and 1
            being "in agreement".
        """
        oai_chat_response = OpenAI().endpoint_openai.run_me(
            lambda: openai.ChatCompletion.create(
                model=self.model_engine,
                temperature=0.0,
                messages=[
                    {
                        "role": "system",
                        "content": feedback_prompts.CORRECT_SYSTEM_PROMPT
                    }, {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )["choices"][0]["message"]["content"]
        )
        agreement_txt = _get_answer_agreement(
            prompt, response, oai_chat_response, self.model_engine
        )
        return _re_1_10_rating(agreement_txt) / 10

    def sentiment(self, text: str) -> float:
        """
        Uses OpenAI's Chat Completion Model. A function that completes a
        template to check the sentiment of some text.

        Parameters:
            text (str): A prompt to an agent. response (str): The agent's
            response to the prompt.

        Returns:
            float: A value between 0 and 1. 0 being "negative sentiment" and 1
            being "positive sentiment".
        """

        return _re_1_10_rating(
            self.endpoint.run_me(
                lambda: openai.ChatCompletion.create(
                    model=self.model_engine,
                    temperature=0.5,
                    messages=[
                        {
                            "role": "system",
                            "content": feedback_prompts.SENTIMENT_SYSTEM_PROMPT
                        }, {
                            "role": "user",
                            "content": text
                        }
                    ]
                )["choices"][0]["message"]["content"]
            )
        )


def _get_answer_agreement(prompt, response, check_response, model_engine):
    print("DEBUG")
    print(feedback_prompts.AGREEMENT_SYSTEM_PROMPT % (prompt, response))
    print("MODEL ANSWER")
    print(check_response)
    oai_chat_response = OpenAI().endpoint.run_me(
        lambda: openai.ChatCompletion.create(
            model=model_engine,
            temperature=0.5,
            messages=[
                {
                    "role":
                        "system",
                    "content":
                        feedback_prompts.AGREEMENT_SYSTEM_PROMPT %
                        (prompt, response)
                }, {
                    "role": "user",
                    "content": check_response
                }
            ]
        )["choices"][0]["message"]["content"]
    )
    return oai_chat_response

# Cannot put these inside Huggingface since it interferes with pydantic.BaseModel.
HUGS_SENTIMENT_API_URL = "https://api-inference.huggingface.co/models/cardiffnlp/twitter-roberta-base-sentiment"
HUGS_TOXIC_API_URL = "https://api-inference.huggingface.co/models/martin-ha/toxic-comment-model"
HUGS_CHAT_API_URL = "https://api-inference.huggingface.co/models/facebook/blenderbot-3B"
HUGS_LANGUAGE_API_URL = "https://api-inference.huggingface.co/models/papluca/xlm-roberta-base-language-detection"

class Huggingface(Provider):

    
    def __init__(self):
        """
        A set of Huggingface Feedback Functions. Utilizes huggingface
        api-inference.
        """

        super().__init__() # need to include pydantic.BaseModel.__init__

        self.endpoint = Endpoint(
            name="huggingface", post_headers=get_huggingface_headers()
        )

    def language_match(self, text1: str, text2: str) -> float:
        """
        Uses Huggingface's papluca/xlm-roberta-base-language-detection model. A
        function that uses language detection on `text1` and `text2` and
        calculates the probit difference on the language detected on text1. The
        function is: `1.0 - (|probit_language_text1(text1) -
        probit_language_text1(text2))`
        
        Parameters:
        
            text1 (str): Text to evaluate.

            text2 (str): Comparative text to evaluate.

        Returns:

            float: A value between 0 and 1. 0 being "different languages" and 1
            being "same languages".
        """

        def get_scores(text):
            payload = {"inputs": text}
            hf_response = self.endpoint.post(
                url=HUGS_LANGUAGE_API_URL, payload=payload, timeout=30
            )
            return {r['label']: r['score'] for r in hf_response}

        max_length = 500
        scores1: AsyncResult[Dict] = TP().promise(
            get_scores, text=text1[:max_length]
        )
        scores2: AsyncResult[Dict] = TP().promise(
            get_scores, text=text2[:max_length]
        )

        scores1: Dict = scores1.get()
        scores2: Dict = scores2.get()

        langs = list(scores1.keys())
        prob1 = np.array([scores1[k] for k in langs])
        prob2 = np.array([scores2[k] for k in langs])
        diff = prob1 - prob2

        l1 = 1.0 - (np.linalg.norm(diff, ord=1)) / 2.0

        return l1

    def positive_sentiment(self, text: str) -> float:
        """
        Uses Huggingface's cardiffnlp/twitter-roberta-base-sentiment model. A
        function that uses a sentiment classifier on `text`.
        
        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "negative sentiment" and 1
            being "positive sentiment".
        """
        max_length = 500
        truncated_text = text[:max_length]
        payload = {"inputs": truncated_text}

        hf_response = self.endpoint.post(
            url=HUGS_SENTIMENT_API_URL, payload=payload
        )

        for label in hf_response:
            if label['label'] == 'LABEL_2':
                return label['score']

    def not_toxic(self, text: str) -> float:
        """
        Uses Huggingface's martin-ha/toxic-comment-model model. A function that
        uses a toxic comment classifier on `text`.
        
        Parameters:
            text (str): Text to evaluate.

        Returns:
            float: A value between 0 and 1. 0 being "toxic" and 1 being "not
            toxic".
        """
        max_length = 500
        truncated_text = text[:max_length]
        payload = {"inputs": truncated_text}
        hf_response = self.endpoint.post(
            url=HUGS_TOXIC_API_URL, payload=payload
        )

        for label in hf_response:
            if label['label'] == 'toxic':
                return label['score']


# cohere
class Cohere(Provider):
    model_engine: str = "large"

    def __init__(self, model_engine='large'):
        super().__init__() # need to include pydantic.BaseModel.__init__

        Cohere().endpoint = Endpoint(name="cohere")
        self.model_engine = model_engine

    def to_json(self) -> Dict:
        return Provider.to_json(self, model_engine=self.model_engine)

    def sentiment(
        self,
        text,
    ):
        return int(
            Cohere().endpoint.run_me(
                lambda: get_cohere_agent().classify(
                    model=self.model_engine,
                    inputs=[text],
                    examples=feedback_prompts.COHERE_SENTIMENT_EXAMPLES
                )[0].prediction
            )
        )

    def not_disinformation(self, text):
        return int(
            Cohere().endpoint.run_me(
                lambda: get_cohere_agent().classify(
                    model=self.model_engine,
                    inputs=[text],
                    examples=feedback_prompts.COHERE_NOT_DISINFORMATION_EXAMPLES
                )[0].prediction
            )
        )
