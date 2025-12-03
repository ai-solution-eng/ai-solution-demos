import type { Metadata } from "next";
import "./globals.css";
import { ToastProvider } from "../context/ToastContext";

export const metadata: Metadata = {
  title: "Justitia & Associates",
  description: "Law Firm Management System",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">
        <ToastProvider>
          {children}
        </ToastProvider>
      </body>
    </html>
  );
}
