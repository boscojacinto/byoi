export const metadata = { title: "BYOI salon project" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "ui-sans-serif, system-ui", margin: "3rem auto", maxWidth: "40rem" }}>
        {children}
      </body>
    </html>
  );
}
