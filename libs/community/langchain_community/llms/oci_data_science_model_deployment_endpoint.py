import json
import logging
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Union,
)

import aiohttp
import requests
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.llms import BaseLLM, create_base_retry_decorator
from langchain_core.load.serializable import Serializable
from langchain_core.outputs import Generation, GenerationChunk, LLMResult
from langchain_core.pydantic_v1 import Field, root_validator
from langchain_core.utils import get_from_dict_or_env

from langchain_community.utilities.requests import Requests

logger = logging.getLogger(__name__)


DEFAULT_TIME_OUT = 300
DEFAULT_CONTENT_TYPE_JSON = "application/json"
DEFAULT_MODEL_NAME = "odsc-llm"


class TokenExpiredError(Exception):
    """Raises when token expired."""

    pass


class ServerError(Exception):
    """Raises when encounter server error when making inference."""

    pass


def _create_retry_decorator(
    llm,
    *,
    run_manager: Optional[
        Union[AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun]
    ] = None,
) -> Callable[[Any], Any]:
    """Create a retry decorator."""
    errors = [requests.exceptions.ConnectTimeout, TokenExpiredError]
    decorator = create_base_retry_decorator(
        error_types=errors, max_retries=llm.max_retries, run_manager=run_manager
    )
    return decorator


class BaseOCIModelDeployment(Serializable):
    """Base class for LLM deployed on OCI Data Science Model Deployment."""

    auth: dict = Field(default_factory=dict, exclude=True)
    """ADS auth dictionary for OCI authentication:
    https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html.
    This can be generated by calling `ads.common.auth.api_keys()`
    or `ads.common.auth.resource_principal()`. If this is not
    provided then the `ads.common.default_signer()` will be used."""

    endpoint: str = ""
    """The uri of the endpoint from the deployed Model Deployment model."""

    streaming: bool = False
    """Whether to stream the results or not."""

    max_retries: int = 3
    """Maximum number of retries to make when generating."""

    @root_validator()
    def validate_environment(  # pylint: disable=no-self-argument
        cls, values: Dict
    ) -> Dict:
        """Validate that python package exists in environment."""
        try:
            import ads

        except ImportError as ex:
            raise ImportError(
                "Could not import ads python package. "
                "Please install it with `pip install oracle_ads`."
            ) from ex

        if not values.get("auth", None):
            values["auth"] = ads.common.auth.default_signer()

        values["endpoint"] = get_from_dict_or_env(
            values,
            "endpoint",
            "OCI_LLM_ENDPOINT",
        )
        return values

    def _headers(self, is_async=False, body=None) -> Dict:
        """Construct and return the headers for a request.

        Args:
            is_async (bool, optional): Indicates if the request is asynchronous.
                Defaults to `False`.
            body (optional): The request body to be included in the headers if
                the request is asynchronous.

        Returns:
            Dict: A dictionary containing the appropriate headers for the request.
        """
        if is_async:
            signer = self.auth["signer"]
            req = requests.Request("POST", self.endpoint, json=body)
            req = req.prepare()
            req = signer(req)
            headers = {}
            for key, value in req.headers.items():
                headers[key] = value

            if self.streaming:
                headers.update(
                    {"enable-streaming": "true", "Accept": "text/event-stream"}
                )
            return headers

        return (
            {
                "Content-Type": DEFAULT_CONTENT_TYPE_JSON,
                "enable-streaming": "true",
                "Accept": "text/event-stream",
            }
            if self.streaming
            else {
                "Content-Type": DEFAULT_CONTENT_TYPE_JSON,
            }
        )

    def completion_with_retry(
        self, run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any
    ) -> Any:
        """Use tenacity to retry the completion call."""
        retry_decorator = _create_retry_decorator(self, run_manager=run_manager)

        @retry_decorator
        def _completion_with_retry(**kwargs: Any) -> Any:
            try:
                request_timeout = kwargs.pop("request_timeout", DEFAULT_TIME_OUT)
                data = kwargs.pop("data")
                stream = kwargs.pop("stream", self.streaming)

                request = Requests(
                    headers=self._headers(), auth=self.auth.get("signer")
                )
                response = request.post(
                    url=self.endpoint,
                    data=data,
                    timeout=request_timeout,
                    stream=stream,
                    **kwargs,
                )
                print("payload\n")
                print(data)
                print("\nkwargs\n")
                print(kwargs)
                self._check_response(response)
                return response
            except TokenExpiredError as e:
                raise e
            except Exception as err:
                logger.debug(
                    f"Requests payload: {data}. Requests arguments: "
                    f"url={self.endpoint},timeout={request_timeout},stream={stream}. "
                    f"Additional request kwargs={kwargs}."
                )
                raise RuntimeError(
                    f"Error occurs by inference endpoint: {str(err)}"
                ) from err

        return _completion_with_retry(**kwargs)

    async def acompletion_with_retry(
        self,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Any:
        """Use tenacity to retry the async completion call."""
        retry_decorator = _create_retry_decorator(self, run_manager=run_manager)

        @retry_decorator
        async def _completion_with_retry(**kwargs: Any) -> Any:
            try:
                request_timeout = kwargs.pop("request_timeout", DEFAULT_TIME_OUT)
                data = kwargs.pop("data")
                stream = kwargs.pop("stream", self.streaming)

                request = Requests(headers=self._headers(is_async=True, body=data))
                if stream:
                    response = request.apost(
                        url=self.endpoint,
                        data=data,
                        timeout=request_timeout,
                    )
                    return self._aiter_sse(response)
                else:
                    async with request.apost(
                        url=self.endpoint,
                        data=data,
                        timeout=request_timeout,
                    ) as response:
                        self._check_response(response)
                        data = await response.json()
                        return data
            except TokenExpiredError as e:
                raise e
            except Exception as err:
                logger.debug(
                    f"Requests payload: `{data}`. "
                    f"Stream mode={stream}. "
                    f"Requests kwargs: url={self.endpoint}, timeout={request_timeout}."
                )
                raise RuntimeError(
                    f"Error occurs by inference endpoint: {str(err)}"
                ) from err

        return await _completion_with_retry(**kwargs)

    def _check_response(
        self, response: Union[requests.Response, aiohttp.ClientResponse]
    ) -> None:
        """Handle server error by checking the response status.

        Args:
            response (Union[requests.Response, aiohttp.ClientResponse]):
                The response object from either `requests` or `aiohttp` library.

        Raises:
            TokenExpiredError:
                If the response status code is 401 and the token refresh is successful.
            ServerError:
                If any other HTTP error occurs.
        """
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as http_err:
            status_code = (
                response.status_code
                if hasattr(response, "status_code")
                else response.status
            )
            if status_code == 401 and self._refresh_signer():
                raise TokenExpiredError() from http_err

            raise ServerError(
                f"Server error: {str(http_err)}. \nMessage: {response.text}"
            ) from http_err

    def _parse_stream(self, lines: Iterator[bytes]) -> Iterator[str]:
        """Parse a stream of byte lines and yield parsed string lines.

        Args:
            lines (Iterator[bytes]):
                An iterator that yields lines in byte format.

        Yields:
            Iterator[str]:
                An iterator that yields parsed lines as strings.
        """
        for line in lines:
            _line = self._parse_stream_line(line)
            if _line is not None:
                yield _line

    async def _parse_stream_async(
        self,
        lines: aiohttp.StreamReader,
    ) -> AsyncIterator[str]:
        """
        Asynchronously parse a stream of byte lines and yield parsed string lines.

        Args:
            lines (aiohttp.StreamReader):
                An `aiohttp.StreamReader` object that yields lines in byte format.

        Yields:
            AsyncIterator[str]:
                An asynchronous iterator that yields parsed lines as strings.
        """
        async for line in lines:
            _line = self._parse_stream_line(line)
            if _line is not None:
                yield _line

    def _parse_stream_line(self, line: bytes) -> Optional[str]:
        """Parse a single byte line and return a processed string line if valid.

        Args:
            line (bytes): A single line in byte format.

        Returns:
            Optional[str]:
                The processed line as a string if valid, otherwise `None`.
        """
        line = line.strip()
        if line:
            line = line.decode("utf-8")
            if "[DONE]" in line:
                return None

            if line.lower().startswith("data:"):
                return line[5:].lstrip()
        return None

    async def _aiter_sse(
        self,
        async_cntx_mgr,
    ) -> AsyncIterator[Dict]:
        """Asynchronously iterate over server-sent events (SSE).

        Args:
            async_cntx_mgr: An asynchronous context manager that yields a client
                response object.

        Yields:
            AsyncIterator[Dict]: An asynchronous iterator that yields parsed server-sent
                event lines as dictionaries.
        """
        async with async_cntx_mgr as client_resp:
            self._check_response(client_resp)
            async for line in self._parse_stream_async(client_resp.content):
                yield line

    def _refresh_signer(self) -> None:
        """Attempt to refresh the security token using the signer.

        Returns:
                bool: `True` if the token was successfully refreshed, `False` otherwise.
        """
        if self.auth.get("signer", None) and hasattr(
            self.auth["signer"], "refresh_security_token"
        ):
            self.auth["signer"].refresh_security_token()
            return True
        return False


class OCIModelDeploymentLLM(BaseLLM, BaseOCIModelDeployment):
    """LLM deployed on OCI Data Science Model Deployment.

    To use, you must provide the model HTTP endpoint from your deployed
    model, e.g. https://modeldeployment.<region>.oci.customer-oci.com/<md_ocid>/predict.

    To authenticate, `oracle-ads` has been used to automatically load
    credentials: https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html

    Make sure to have the required policies to access the OCI Data
    Science Model Deployment endpoint. See:
    https://docs.oracle.com/en-us/iaas/data-science/using/model-dep-policies-auth.htm#model_dep_policies_auth__predict-endpoint

    Example:

        .. code-block:: python

            from langchain_community.llms import OCIModelDeploymentLLM

            llm = OCIModelDeploymentLLM(
                endpoint="https://modeldeployment.us-ashburn-1.oci.customer-oci.com/<ocid>/predict",
                model="odsc-llm",
                streaming=True,
                model_kwargs={"frequency_penalty": 1.0},
            )
            llm.invoke("tell me a joke.")

        Customized Usage:

        User can inherit from our base class and overrwrite the `_process_response`, `_process_stream_response`,
        `_construct_json_body` for satisfying customized needed.

        .. code-block:: python

            from langchain_community.llms import OCIModelDeploymentLLM

            class MyCutomizedModel(OCIModelDeploymentLLM):
                def _process_stream_response(self, response_json:dict) -> GenerationChunk:
                    print("My customized output stream handler.")
                    return GenerationChunk()

                def _process_response(self, response_json:dict) -> List[Generation]:
                    print("My customized output handler.")
                    return [Generation()]

                def _construct_json_body(self, prompt: str, param:dict) -> dict:
                    print("My customized input handler.")
                    return {}

            llm = MyCutomizedModel(
                endpoint=f"https://modeldeployment.us-ashburn-1.oci.customer-oci.com/{ocid}/predict",
                model="<model_name>",
            }

            llm.invoke("tell me a joke.")

    """  # noqa: E501

    model: str = DEFAULT_MODEL_NAME
    """The name of the model."""

    max_tokens: int = 256
    """Denotes the number of tokens to predict per generation."""

    temperature: float = 0.2
    """A non-negative float that tunes the degree of randomness in generation."""

    k: int = -1
    """Number of most likely tokens to consider at each step."""

    p: float = 0.75
    """Total probability mass of tokens to consider at each step."""

    best_of: int = 1
    """Generates best_of completions server-side and returns the "best"
    (the one with the highest log probability per token).
    """

    stop: Optional[List[str]] = None
    """Stop words to use when generating. Model output is cut off
    at the first occurrence of any of these substrings."""

    model_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Keyword arguments to pass to the model."""

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_model_deployment_endpoint"

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether this model can be serialized by Langchain."""
        return True

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters."""
        return {
            "best_of": self.best_of,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "stop": self.stop,
            "stream": self.streaming,
            "temperature": self.temperature,
            "top_k": self.k,
            "top_p": self.p,
        }

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        _model_kwargs = self.model_kwargs or {}
        return {
            **{"endpoint": self.endpoint},
            **{"model_kwargs": _model_kwargs},
            **self._default_params,
        }

    def _generate(
        self,
        prompts: List[str],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Call out to OCI Data Science Model Deployment endpoint with k unique prompts.

        Args:
            prompts: The prompts to pass into the service.
            stop: Optional list of stop words to use when generating.

        Returns:
            The full LLM output.

        Example:
            .. code-block:: python

                response = llm.invoke("Tell me a joke.")
                response = llm.generate(["Tell me a joke."])
        """
        prompts = [prompts] if isinstance(prompts, str) else prompts
        generations: List[List[Generation]] = []
        params = self._invocation_params(stop, **kwargs)
        for prompt in prompts:
            body = self._construct_json_body(prompt, params)
            if self.streaming:
                generation = GenerationChunk(text="")
                for chunk in self._stream(
                    prompt, stop=stop, run_manager=run_manager, **kwargs
                ):
                    generation += chunk
                generations.append([generation])
            else:
                res = self.completion_with_retry(
                    data=body,
                    run_manager=run_manager,
                    **kwargs,
                )
                generations.append(self._process_response(res.json()))
        return LLMResult(generations=generations)

    async def _agenerate(
        self,
        prompts: List[str],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Call out to OCI Data Science Model Deployment endpoint async with k unique prompts.

        Args:
            prompts: The prompts to pass into the service.
            stop: Optional list of stop words to use when generating.

        Returns:
            The full LLM output.

        Example:
            .. code-block:: python

                response = await llm.ainvoke("Tell me a joke.")
                response = await llm.agenerate(["Tell me a joke."])
        """  # noqa: E501
        prompts = [prompts] if isinstance(prompts, str) else prompts
        generations: List[List[Generation]] = []
        params = self._invocation_params(stop, **kwargs)
        for prompt in prompts:
            body = self._construct_json_body(prompt, params)
            if self.streaming:
                generation = GenerationChunk(text="")
                async for chunk in self._astream(
                    prompt, stop=stop, run_manager=run_manager, **kwargs
                ):
                    generation += chunk
                generations.append([generation])
            else:
                res = await self.acompletion_with_retry(
                    data=body,
                    run_manager=run_manager,
                    **kwargs,
                )
                generations.append(self._process_response(res))
        return LLMResult(generations=generations)

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        """Stream OCI Data Science Model Deployment endpoint on given prompt.


        Args:
            prompt (str):
                The prompt to pass into the model.
            stop (List[str], Optional):
                List of stop words to use when generating.
            kwargs:
                requests_kwargs:
                    Additional ``**kwargs`` to pass to requests.post

        Returns:
            An iterator of GenerationChunks.


        Example:

            .. code-block:: python

            response = llm.stream("Tell me a joke.")

        """
        requests_kwargs = kwargs.pop("requests_kwargs", {})
        self.streaming = True
        params = self._invocation_params(stop, **kwargs)
        body = self._construct_json_body(prompt, params)

        response = self.completion_with_retry(
            data=body, run_manager=run_manager, stream=True, **requests_kwargs
        )

        for line in self._parse_stream(response.iter_lines()):
            chunk = self._handle_sse_line(line)
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    async def _astream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenerationChunk]:
        """Stream OCI Data Science Model Deployment endpoint async on given prompt.


        Args:
            prompt (str):
                The prompt to pass into the model.
            stop (List[str], Optional):
                List of stop words to use when generating.
            kwargs:
                requests_kwargs:
                    Additional ``**kwargs`` to pass to requests.post

        Returns:
            An iterator of GenerationChunks.


        Example:

            .. code-block:: python

            async for chunk in llm.astream(("Tell me a joke."):
                print(chunk, end="", flush=True)

        """
        requests_kwargs = kwargs.pop("requests_kwargs", {})
        self.streaming = True
        params = self._invocation_params(stop, **kwargs)
        body = self._construct_json_body(prompt, params)

        async for line in await self.acompletion_with_retry(
            data=body, run_manager=run_manager, stream=True, **requests_kwargs
        ):
            chunk = self._handle_sse_line(line)
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    def _construct_json_body(self, prompt: str, params: dict) -> dict:
        """Constructs the request body as a dictionary (JSON)."""
        return {
            "prompt": prompt,
            **params,
        }

    def _invocation_params(
        self, stop: Optional[List[str]] = None, **kwargs: Any
    ) -> dict:
        """Combines the invocation parameters with default parameters."""
        params = self._default_params
        _model_kwargs = self.model_kwargs or {}
        params["stop"] = stop or params.get("stop", [])
        return {**params, **_model_kwargs, **kwargs}

    def _process_stream_response(self, response_json: dict) -> GenerationChunk:
        """Formats streaming response for OpenAI spec into GenerationChunk."""
        try:
            choice = response_json["choices"][0]
            if not isinstance(choice, dict):
                raise TypeError("Endpoint response is not well formed.")
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("Error while formatting response payload.") from e

        return GenerationChunk(text=choice.get("text", ""))

    def _process_response(self, response_json: dict) -> List[Generation]:
        """Formats response in OpenAI spec.

        Args:
            response_json (dict): The JSON response from the chat model endpoint.

        Returns:
            ChatResult: An object containing the list of `ChatGeneration` objects
            and additional LLM output information.

        Raises:
            ValueError: If the response JSON is not well-formed or does not
            contain the expected structure.

        """
        generations = []
        try:
            choices = response_json["choices"]
            if not isinstance(choices, list):
                raise TypeError("Endpoint response is not well formed.")
        except (KeyError, TypeError) as e:
            raise ValueError("Error while formatting response payload.") from e

        for choice in choices:
            gen = Generation(
                text=choice.get("text"),
                generation_info=self._generate_info(choice),
            )
            generations.append(gen)

        return generations

    def _generate_info(self, choice: dict) -> dict:
        """Extracts generation info from the response."""
        gen_info = {}
        finish_reason = choice.get("finish_reason", None)
        logprobs = choice.get("logprobs", None)
        index = choice.get("index", None)
        if finish_reason:
            gen_info.update({"finish_reason": finish_reason})
        if logprobs is not None:
            gen_info.update({"logprobs": logprobs})
        if index is not None:
            gen_info.update({"index": index})

        return gen_info or None

    def _handle_sse_line(self, line: str) -> GenerationChunk:
        try:
            obj = json.loads(line)
            return self._process_stream_response(obj)
        except Exception:
            return GenerationChunk()


class OCIModelDeploymentTGI(OCIModelDeploymentLLM):
    """OCI Data Science Model Deployment TGI Endpoint.

    To use, you must provide the model HTTP endpoint from your deployed
    model, e.g. https://modeldeployment.<region>.oci.customer-oci.com/<md_ocid>/predict.

    To authenticate, `oracle-ads` has been used to automatically load
    credentials: https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html

    Make sure to have the required policies to access the OCI Data
    Science Model Deployment endpoint. See:
    https://docs.oracle.com/en-us/iaas/data-science/using/model-dep-policies-auth.htm#model_dep_policies_auth__predict-endpoint

    Example:
        .. code-block:: python

            from langchain_community.llms import OCIModelDeploymentTGI

            llm = OCIModelDeploymentTGI(
                endpoint="https://modeldeployment.<region>.oci.customer-oci.com/<md_ocid>/predict",
                api="/v1/completions",
                streaming=True,
                temperature=0.2,
                seed=42,
                # other model parameters ...
            )

    """

    api: Literal["/generate", "/v1/completions"] = "/generate"
    """Api spec."""

    frequency_penalty: float = 0.0
    """Penalizes repeated tokens according to frequency. Between 0 and 1."""

    seed: Optional[int] = None
    """Random sampling seed"""

    repetition_penalty: Optional[float] = None
    """The parameter for repetition penalty. 1.0 means no penalty."""

    suffix: Optional[str] = None
    """The text to append to the prompt. """

    do_sample: bool = True
    """If set to True, this parameter enables decoding strategies such as
    multi-nominal sampling, beam-search multi-nominal sampling, Top-K
    sampling and Top-p sampling.
    """

    watermark = True
    """Watermarking with `A Watermark for Large Language Models <https://arxiv.org/abs/2301.10226>`_.
    Defaults to True."""

    return_full_text = False
    """Whether to prepend the prompt to the generated text. Defaults to False."""

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_model_deployment_tgi_endpoint"

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for invoking OCI model deployment TGI endpoint."""
        return (
            {
                "model": self.model,  # can be any
                "frequency_penalty": self.frequency_penalty,
                "max_tokens": self.max_tokens,
                "repetition_penalty": self.repetition_penalty,
                "temperature": self.temperature,
                "top_p": self.p,
                "seed": self.seed,
                "stream": self.streaming,
                "suffix": self.suffix,
                "stop": self.stop,
            }
            if self.api == "/v1/completions"
            else {
                "best_of": self.best_of,
                "max_new_tokens": self.max_tokens,
                "temperature": self.temperature,
                "top_k": (
                    self.k if self.k > 0 else None
                ),  # `top_k` must be strictly positive'
                "top_p": self.p,
                "do_sample": self.do_sample,
                "return_full_text": self.return_full_text,
                "watermark": self.watermark,
                "stop": self.stop,
            }
        )

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        _model_kwargs = self.model_kwargs or {}
        return {
            **{
                "endpoint": self.endpoint,
                "api": self.api,
                "model_kwargs": _model_kwargs,
            },
            **self._default_params,
        }

    def _construct_json_body(self, prompt: str, params: dict) -> dict:
        """Construct request payload."""
        if self.api == "/v1/completions":
            return super()._construct_json_body(prompt, params)

        return {
            "inputs": prompt,
            "parameters": params,
        }

    def _process_response(self, response_json: dict) -> List[Generation]:
        """Formats response."""
        if self.api == "/v1/completions":
            return super()._process_response(response_json)

        try:
            text = response_json["generated_text"]
        except KeyError as e:
            raise ValueError(
                f"Error while formatting response payload.response_json={response_json}"
            ) from e

        return [Generation(text=text)]


class OCIModelDeploymentVLLM(OCIModelDeploymentLLM):
    """VLLM deployed on OCI Data Science Model Deployment

    To use, you must provide the model HTTP endpoint from your deployed
    model, e.g. https://modeldeployment.<region>.oci.customer-oci.com/<md_ocid>/predict.

    To authenticate, `oracle-ads` has been used to automatically load
    credentials: https://accelerated-data-science.readthedocs.io/en/latest/user_guide/cli/authentication.html

    Make sure to have the required policies to access the OCI Data
    Science Model Deployment endpoint. See:
    https://docs.oracle.com/en-us/iaas/data-science/using/model-dep-policies-auth.htm#model_dep_policies_auth__predict-endpoint

    Example:
        .. code-block:: python

            from langchain_community.llms import OCIModelDeploymentVLLM

            llm = OCIModelDeploymentVLLM(
                endpoint="https://modeldeployment.<region>.oci.customer-oci.com/<md_ocid>/predict",
                model="odsc-llm",
                streaming=False,
                temperature=0.2,
                max_tokens=512,
                n=3,
                best_of=3,
                # other model parameters
            )

    """

    n: int = 1
    """Number of output sequences to return for the given prompt."""

    k: int = -1
    """Number of most likely tokens to consider at each step."""

    frequency_penalty: float = 0.0
    """Penalizes repeated tokens according to frequency. Between 0 and 1."""

    presence_penalty: float = 0.0
    """Penalizes repeated tokens. Between 0 and 1."""

    use_beam_search: bool = False
    """Whether to use beam search instead of sampling."""

    ignore_eos: bool = False
    """Whether to ignore the EOS token and continue generating tokens after
    the EOS token is generated."""

    logprobs: Optional[int] = None
    """Number of log probabilities to return per output token."""

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "oci_model_deployment_vllm_endpoint"

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling vllm."""
        return {
            "best_of": self.best_of,
            "frequency_penalty": self.frequency_penalty,
            "ignore_eos": self.ignore_eos,
            "logprobs": self.logprobs,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "n": self.n,
            "presence_penalty": self.presence_penalty,
            "stop": self.stop,
            "stream": self.streaming,
            "temperature": self.temperature,
            "top_k": self.k,
            "top_p": self.p,
            "use_beam_search": self.use_beam_search,
        }
