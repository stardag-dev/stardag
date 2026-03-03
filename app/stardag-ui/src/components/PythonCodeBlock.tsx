import { useState, useEffect, useRef } from "react";
import { createHighlighter, type Highlighter } from "shiki";

// Singleton highlighter — loaded once, reused everywhere
let highlighterPromise: Promise<Highlighter> | null = null;
function getHighlighter() {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ["github-dark"],
      langs: ["python"],
    });
  }
  return highlighterPromise;
}

function usePythonHighlight(code: string) {
  const [html, setHtml] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    getHighlighter().then((highlighter) => {
      if (!mountedRef.current) return;
      setHtml(
        highlighter.codeToHtml(code, {
          lang: "python",
          theme: "github-dark",
        }),
      );
    });
    return () => {
      mountedRef.current = false;
    };
  }, [code]);

  return html;
}

export function PythonCodeBlock({ code }: { code: string }) {
  const html = usePythonHighlight(code);

  if (!html) {
    return (
      <pre className="m-0 bg-transparent p-4 text-left text-xs leading-relaxed">
        <code className="text-gray-400">{code}</code>
      </pre>
    );
  }

  return (
    <div
      className="overflow-x-auto p-4 text-left text-xs leading-relaxed [&_pre]:!m-0 [&_pre]:!bg-transparent [&_pre]:!p-0"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
