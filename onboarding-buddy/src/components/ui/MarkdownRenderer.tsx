import React from 'react';
import ReactMarkdown, { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { okaidia } from 'react-syntax-highlighter/dist/esm/styles/prism';

type MarkdownComponents = Partial<{
  [TagName in keyof JSX.IntrinsicElements]: React.ComponentType<{
    children?: React.ReactNode;
    node?: any;
    className?: string;
    [key: string]: any;
  }>;
}>;

interface MarkdownRendererProps {
  content: string;
}

export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({ content }) => {
  if (!content) return null;

  const components: MarkdownComponents = {
    h1: ({ children, ...props }) => (
      <h1 className="text-2xl font-bold mt-8 mb-4" {...props}>
        {children}
      </h1>
    ),
    h2: ({ children, ...props }) => (
      <h2 className="text-xl font-bold mt-6 mb-3" {...props}>
        {children}
      </h2>
    ),
    h3: ({ children, ...props }) => (
      <h3 className="text-lg font-semibold mt-4 mb-2" {...props}>
        {children}
      </h3>
    ),
    p: ({ children, ...props }) => (
      <p className="mb-4" {...props}>
        {children}
      </p>
    ),
    ul: ({ children, ...props }) => (
      <ul className="list-disc ml-5 mb-4 space-y-1" {...props}>
        {children}
      </ul>
    ),
    ol: ({ children, ...props }) => (
      <ol className="list-decimal ml-5 mb-4 space-y-1" {...props}>
        {children}
      </ol>
    ),
    a: ({ children, ...props }) => (
      <a
        className="text-blue-500 hover:underline"
        target="_blank"
        rel="noopener noreferrer"
        {...props}
      >
        {children}
      </a>
    ),
    code: ({ node, className, children, ...props }) => {
      const match = /language-(\w+)/.exec(className || '');
      const isInline = !className?.includes('language-');
      
      return isInline ? (
        <code className="bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded text-sm font-mono">
          {children}
        </code>
      ) : (
        <SyntaxHighlighter
          style={okaidia}
          language={match ? match[1] : 'text'}
          PreTag="div"
          className="rounded-lg my-4"
          {...props as any}
        >
          {String(children).replace(/\n$/, '')}
        </SyntaxHighlighter>
      );
    },
    table: ({ children, ...props }) => (
      <div className="overflow-x-auto">
        <table className="min-w-full border-collapse border border-gray-300 dark:border-gray-700 my-4" {...props}>
          {children}
        </table>
      </div>
    ),
    th: ({ children, ...props }) => (
      <th className="border border-gray-300 dark:border-gray-700 px-4 py-2 text-left bg-gray-100 dark:bg-gray-800" {...props}>
        {children}
      </th>
    ),
    td: ({ children, ...props }) => (
      <td className="border border-gray-300 dark:border-gray-700 px-4 py-2" {...props}>
        {children}
      </td>
    ),
    blockquote: ({ children, ...props }) => (
      <blockquote className="border-l-4 border-gray-300 dark:border-gray-600 pl-4 italic my-4" {...props}>
        {children}
      </blockquote>
    ),
  };

  return (
    <div className="prose dark:prose-invert max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components as Components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownRenderer;
