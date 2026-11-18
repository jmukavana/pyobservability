from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanLimits, SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NoOpTracerProvider, Tracer, TracerProvider

from pyobservability.config import ObservabilityConfig, ResolvedProviderMode
from pyobservability.core.resource import build_resource
from pyobservability.core.timeouts import to_millis
from pyobservability.exporters import span_exporters
from pyobservability.security import Redactor
from pyobservability.tracing.provider import global_delegating_provider
from pyobservability.tracing.redaction import RedactingSpanExporter
from pyobservability.tracing.sampling import sampler_for


def get_tracer(name: str, version: str | None = None) -> Tracer:
    return trace.get_tracer(name, version)


class DetachableSpanProcessor(SpanProcessor):
    """OpenTelemetry cannot remove a processor from a provider; this one can switch itself off."""

    def __init__(self, processor: SpanProcessor) -> None:
        self._processor: SpanProcessor | None = processor

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        if self._processor is not None:
            self._processor.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if self._processor is not None:
            self._processor.on_end(span)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._processor is None or self._processor.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self.detach()

    def detach(self) -> None:
        processor, self._processor = self._processor, None
        if processor is not None:
            processor.shutdown()


class TracingComponent:
    name = "tracing"

    def __init__(
        self,
        config: ObservabilityConfig,
        redactor: Redactor,
        provider_mode: ResolvedProviderMode,
        global_provider: TracerProvider,
    ) -> None:
        self._config = config
        self._redactor = redactor
        self._provider_mode = provider_mode
        self._global_provider = global_provider
        self._own_provider: SdkTracerProvider | None = None
        self._attached: DetachableSpanProcessor | None = None

    def start(self) -> None:
        match self._provider_mode:
            case "own":
                self._own_provider = self._new_sdk_provider()
                global_delegating_provider().delegate_to(self._own_provider)
            case "attach":
                self._attach_to_global_provider()
            case "external":
                pass

    def shutdown(self, timeout_s: float) -> None:
        timeout_millis = to_millis(timeout_s)
        if self._own_provider is not None:
            global_delegating_provider().delegate_to(NoOpTracerProvider())
            self._own_provider.force_flush(timeout_millis)
            self._own_provider.shutdown()
        if self._attached is not None:
            self._attached.force_flush(timeout_millis)
            self._attached.detach()

    def _new_sdk_provider(self) -> SdkTracerProvider:
        tracing = self._config.tracing
        provider = SdkTracerProvider(
            resource=build_resource(self._config),
            sampler=sampler_for(tracing),
            span_limits=SpanLimits(max_span_attributes=tracing.span_attribute_limit),
        )
        if processor := self._export_processor():
            provider.add_span_processor(processor)
        return provider

    def _attach_to_global_provider(self) -> None:
        processor = self._export_processor()
        if processor is None or not isinstance(self._global_provider, SdkTracerProvider):
            return
        self._attached = DetachableSpanProcessor(processor)
        self._global_provider.add_span_processor(self._attached)

    def _export_processor(self) -> SpanProcessor | None:
        tracing = self._config.tracing
        if not tracing.exports:
            return None
        exporter = span_exporters.create(tracing.exporter, config=tracing)
        return BatchSpanProcessor(
            RedactingSpanExporter(
                exporter,
                self._redactor,
                template_paths=self._config.security.path_param_redaction,
            ),
            max_queue_size=tracing.max_queue_size,
            schedule_delay_millis=tracing.schedule_delay_ms,
            max_export_batch_size=tracing.max_export_batch_size,
            export_timeout_millis=tracing.export_timeout_ms,
        )
