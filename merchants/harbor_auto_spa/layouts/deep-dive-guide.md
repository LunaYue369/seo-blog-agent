# Ultimate Deep-Dive Guide

## Content Layout: Ultimate Deep-Dive Guide

You MUST follow this layout structure:
1. Open with authority: establish why this guide exists and who it's for
2. Include a "Table of Contents" as a bullet list with the section names after the intro
3. Use **H2 for major chapters**, H3 for sub-topics — create a proper guide hierarchy
4. Each major section should have at least one of: table, list, blockquote, or highlight box
5. Include a <div class="blog-section"> "Key Takeaway" card at the end of each H2 section
6. Use <div class="blog-highlight"> for "Expert Insight" callouts (at least 2)
7. Aim for longer, more detailed content — this is a comprehensive resource
8. End with a summary section (H2) that recaps the most important points before the CTA

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
