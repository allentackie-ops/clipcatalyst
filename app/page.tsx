import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import Hero from "@/components/sections/Hero";
import Problem from "@/components/sections/Problem";
import ViralityEngine from "@/components/sections/ViralityEngine";
import Speed from "@/components/sections/Speed";
import ChatEditing from "@/components/sections/ChatEditing";
import Features from "@/components/sections/Features";
import Pipeline from "@/components/sections/Pipeline";
import HowItWorks from "@/components/sections/HowItWorks";
import Pricing from "@/components/sections/Pricing";
import Comparison from "@/components/sections/Comparison";
import FAQ from "@/components/sections/FAQ";
import FinalCTA from "@/components/sections/FinalCTA";

export default function Home() {
  return (
    <>
      <Navbar />
      <main className="pt-16">
        <Hero />
        <Problem />
        <ViralityEngine />
        <Speed />
        <ChatEditing />
        <Features />
        <Pipeline />
        <HowItWorks />
        <Pricing />
        <Comparison />
        <FAQ />
        <FinalCTA />
      </main>
      <Footer />
    </>
  );
}
