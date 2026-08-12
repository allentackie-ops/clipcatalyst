import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import AccountProvider from "@/components/account/AccountProvider";
import AccountApp from "@/components/account/AccountApp";

export const metadata: Metadata = {
  title: "Account | ClipCatalyst",
  description:
    "Your ClipCatalyst plan, this month's clip usage, and billing, all in one place.",
};

export default function AccountPage() {
  return (
    <>
      <Navbar />
      <main id="main" className="pt-16">
        <AccountProvider>
          <AccountApp />
        </AccountProvider>
      </main>
    </>
  );
}
