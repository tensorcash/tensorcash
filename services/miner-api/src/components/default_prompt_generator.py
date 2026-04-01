import random
import string

class IntelligentPromptGenerator:
    def __init__(self):
        self.templates = [
            "Write a story about a {trait} {occupation} who discovers {event} in {setting}.",
            "Describe a {adjective} device designed to {function}, found in a {time_period} marketplace.",
            "Design a storyline for a {business_type} that emphasizes {aesthetic_style} in its branding.",
            "Imagine a {tone} conversation between a {role_1} and a {role_2} in {location}.",
        ]

        self.word_bank = {
            "trait": ["curious", "lazy", "obsessive", "naive", "vindictive", "idealistic"],
            "occupation": ["chef", "astronaut", "jeweler", "street magician", "crypto miner"],
            "event": ["a hidden portal", "a time loop", "a haunted mirror", "a talking fox"],
            "setting": ["an underwater city", "a deserted space station", "a post-apocalyptic museum"],

            "adjective": ["shimmering", "rusty", "modular", "organic"],
            "function": ["store dreams", "translate animal speech", "measure nostalgia", "synthesize joy"],
            "time_period": ["neo-Victorian", "retrofuturist", "solar punk", "post-human"],

            "business_type": ["AI startup", "sustainable fashion label", "coffee shop in space"],
            "aesthetic_style": ["brutalist", "vaporwave", "minimalist", "biomorphic"],

            "tone": ["whimsical", "existential", "dark", "absurd"],
            "role_1": ["robot therapist", "17th-century mathematician", "child AI trainer"],
            "role_2": ["time-traveling snail", "retired hacker", "cosmic librarian"],
            "location": ["a glitching metaverse café", "a lunar subway tunnel", "a memory vault"],
        }

    def generate_prompt(self, template: str = None) -> str:
        # Pick a random template if none provided
        tmpl = template or random.choice(self.templates)

        # Extract all field names from the template
        formatter = string.Formatter()
        fields = [fname for _, fname, _, _ in formatter.parse(tmpl) if fname]

        # Build a dict of replacements
        values = {}
        for field in fields:
            if field in self.word_bank:
                values[field] = random.choice(self.word_bank[field])
            else:
                # fallback for missing categories
                values[field] = f"<missing_{field}>"

        # Return the fully formatted prompt
        return tmpl.format(**values)

    def list_templates(self):
        return list(self.templates)

    def add_template(self, template: str):
        self.templates.append(template)

    def add_word(self, category: str, word: str):
        self.word_bank.setdefault(category, []).append(word)

    def list_categories(self):
        return list(self.word_bank.keys())

    def list_words(self, category: str):
        return list(self.word_bank.get(category, []))