# Numbered Listicle / Tips

## Content Layout: Numbered Listicle / Tips

You MUST follow this layout structure:
1. Open with a bold claim: "X Tips That Will..." or "The Top X..."
2. Each **H2 is a numbered tip/item** (e.g., "1. Check Your Filters Monthly")
3. Keep each tip section SHORT (150-200 words max) — scannable and punchy
4. Alternate between paragraphs, bullet lists, and <div class="blog-section"> cards
5. Include at least ONE table (cost comparison, before/after, specs)
6. Add a <div class="blog-highlight"> "Bonus Tip" near the end
7. Use bold text for the key takeaway sentence in each tip
8. Number your tips with H2 so the article has clear visual rhythm

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
