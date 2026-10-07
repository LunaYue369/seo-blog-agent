# How-To Guide

## Content Layout: Step-by-Step How-To Guide

You MUST follow this layout structure:
1. Open with the end result the reader will achieve (paint a picture of success)
2. Include a "What You'll Need / What to Know First" box using <div class="blog-highlight">
3. Use NUMBERED STEPS — each major step is an **H2 heading**, sub-steps use H3
4. Add a "Pro Tip" blockquote after at least 2 steps
5. End with a "Common Mistakes to Avoid" section (H2) before the CTA
6. Use short, imperative sentences ("Check the...", "Make sure to...")
7. Include at least one comparison table showing right vs wrong approaches

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
