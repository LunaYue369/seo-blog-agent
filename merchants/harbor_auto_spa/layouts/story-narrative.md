# 故事叙事型 (Story-Driven Narrative)

## Content Layout: Story-Driven Narrative

You MUST follow this layout structure:
1. Open with a vivid, specific SCENARIO the reader can relate to (use second person "you")
2. Build tension — describe the problem getting worse, the cost of inaction
3. Introduce the solution naturally through the story (not as a sales pitch)
4. Use blockquotes for "customer perspective" moments or expert quotes
5. Weave in facts and data WITHIN the narrative — don't break into listicle mode
6. Include ONE mid-article <div class="blog-highlight"> for a key insight
7. Use 3-4 **H2 sections** to structure the story arc — let the story flow but keep SEO structure
8. CTA should feel like the natural next chapter of the story

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
