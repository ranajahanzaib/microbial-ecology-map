import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "Microbial Atlas — Environmental microbiomes",
  description:
    "Explore verified environmental microbiome records and their provenance.",
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
