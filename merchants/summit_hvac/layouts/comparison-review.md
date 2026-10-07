# Comparison & Review

## Content Layout: Comparison & Review

You MUST follow this layout structure:
1. Open with the reader's dilemma — "Choosing between X and Y? You're not alone."
2. Include a quick "At a Glance" comparison TABLE right after the intro (3-5 criteria)
3. Dedicate one **H2 section** per option/product being compared — balanced, fair analysis
4. Use <div class="blog-section"> cards to summarize pros and cons of each option
5. Include a "Best For..." recommendation section (H2) — match options to reader profiles
6. Add real numbers: prices, specs, durations, ratings where possible
7. End with "Our Recommendation" (H2) + CTA — help the reader decide, don't just list facts

## Mandatory SEO Rules
- **Heading hierarchy:** Only ONE <h1> (the article title, handled by the template). ALL section headings MUST use <h2>. Sub-sections use <h3>. Never skip levels.
- **H2 keyword optimization:** Each <h2> heading MUST contain a secondary keyword or long-tail search phrase relevant to the topic. Aim for 4-7 H2 sections.
- **First 100 words:** The primary keyword MUST appear within the first 100 words of the article.
- **FAQ section (REQUIRED):** End every article with a FAQ section BEFORE the CTA. Use this HTML structure:
  ```html
  <div class="faq-section">
    <h2>Frequently Asked Questions</h2>
    <div class="faq-item"><h3>Question here?</h3><p>Answer here.</p></div>
    <!-- 3-5 Q&A pairs, questions should match "People Also Ask" style queries -->
  </div>
  ```
- **Internal links:** Include 3-5 internal links to the business website pages (services, about, contact). Use keyword-rich anchor text, NOT "click here".
- **External links:** Include 1-2 links to authoritative external sources (industry organizations, manufacturer sites, government resources) to build topical trust.
- **Image alt text:** All image placeholders (<!-- BLOG_IMAGE:hero/mid/end -->) should have descriptive alt text that naturally includes a keyword.
- **Tags:** Every article MUST include 3-5 relevant tags in the output JSON.
- **Meta description (excerpt):** MUST be 150-160 characters, include the primary keyword, and have a compelling CTA.
