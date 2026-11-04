"""Question wording aliases mapped to verified candidate-answer keys.

Aliases describe fields, not new candidate facts. Answers are read from the
private registry and are used only while the relevant record is verified.
"""
ALIASES = {
 'first_name': ['First name','Given name','Legal first name','Applicant first name','What is your first name'],
 'middle_name': ['Middle name','Applicant middle name','What is your middle name'],
 'last_name': ['Last name','Surname','Family name','Legal last name','Applicant last name','What is your last name'],
 'full_name': ['Full name','Your name','Applicant name','Candidate name','Name of applicant','Name'],
 'preferred_name': ['Preferred name','Preferred name/Alias','Preferred name or alias','What is your preferred name'],
 'application_email': ['Email','Email address','Contact email','Contact email address','Email ID','Preferred email','Applicant email','What is your email address'],
 'phone_number': ['Phone','Phone number','Mobile number','Contact number','Mobile phone','Telephone number','Your phone number','Applicant phone number'],
 'phone_country_code': ['Phone country code','Country calling code','Telephone country code','Phone number country code'],
 'current_city': ['City','Current city','City of residence','Current location','What is your current location','Where are you currently located','Current city of residence','Location'],
 'state': ['State','State of residence','State province','Province','Current state'],
 'country': ['Country','Country of residence','Current country of residence','What is your current country of residence','Current country','Where do you currently reside'],
 'linkedin': ['LinkedIn','LinkedIn URL','LinkedIn profile URL','LinkedIn profile','Link to your LinkedIn profile','Please enter your LinkedIn profile URL'],
 'website': ['Portfolio','Portfolio URL','Website','Personal website','Portfolio link','Online portfolio URL','Please enter your online portfolio URL'],
 'current_employer': ['Current employer','Current company','Current organization','Name of current employer','Most recent employer','Present employer'],
 'current_role': ['Current role','Current job title','Current position','Present job title','Current designation','Current title'],
 'current_employment_type': ['Current employment type','Current job type','Employment type of current role'],
 'education_degree': ['Degree','Highest degree','Highest qualification','Highest educational qualification','Education degree','Highest completed qualification','Degree qualification'],
 'bachelors_completed': ["Have you completed the following level of education: Bachelor's Degree",'Have you completed a bachelor degree','Have you completed a bachelor\'s degree','Do you have a completed bachelor\'s degree'],
 'education_institution': ['Institution name','Undergraduate institution name','College name','College','Name of institution','Name of college','Undergraduate college','Undergraduate institution'],
 'education_university': ['University','University name','Name of university','Undergraduate university','University attended'],
 'graduation_year': ['Graduation year','Year of graduation','Year graduated','Graduation completion year','Year of degree completion','Graduating year'],
 'education_percentage': ['Graduation percentage','Degree percentage','Education percentage','Academic percentage','Undergraduate percentage','College percentage'],
 'total_experience_years': ['Years of experience','Total experience','Total years of experience','Years of professional experience','Total work experience','Professional experience','How many years of professional experience do you have','Overall years of experience','Total professional experience','Total years of professional experience','Total years of work experience','How many years of Engineering experience do you currently have','How many years of professional development experience do you have','How many years of software development experience do you have'],
 'additional_experience_months': ['Additional months of experience','Additional experience in months','Total additional months of experience','Additional months beyond full years','Total additional months of professional experience'],
 'nodejs_experience_years': ['Years of Node.js experience','Node.js experience in years','Years using Node.js','Professional Node.js experience in years','How many years of experience do you have with Node.js'],
 'postgresql_experience_years': ['Years of PostgreSQL experience','PostgreSQL experience in years','Years using PostgreSQL','Professional PostgreSQL experience in years','How many years of experience do you have with PostgreSQL'],
 'reactjs_experience_years': ['Years of React.js experience','React.js experience in years','Years using React','Professional React experience in years','How many years of experience do you have with React.js'],
 'nextjs_experience_years': ['Years of Next.js and React experience','Next.js and React experience in years','How many years of hands-on experience do you have with Next.js and React'],
 'fullstack_experience_years': ['Years of full stack experience','Full stack experience in years','Years of full-stack development experience','Professional full stack experience in years','How many years of full-stack development experience do you have','How many years of experience do you have working on the backend'],
 'python_experience_years': ['Years of Python experience','Python experience in years','Years using Python','Professional Python experience in years','How many years of experience do you have in Python','How many years of work experience do you have with Python programming language'],
 'generative_ai_experience_years': ['Years of generative AI experience','Generative AI experience in years','Gen AI experience in years','Professional generative AI experience in years','How many years of experience you have with Gen AI','Years of LLM experience','LLM experience in years','Years of RAG experience','RAG experience in years','How many years of work experience do you have with LLM/RAG','How many years of experience do you have with LLMs and RAG'],
 'ai_coding_assistant_experience_years': ['Years of AI-assisted coding experience','AI coding assistant experience in years','Cursor experience in years','Claude coding experience in years','How many years of work experience do you have with AI Driven Development using Cursor/Claude','How many years of experience do you have using Cursor or Claude for development'],
 'generative_ai_knowledge': ['Do you have knowledge on GenAI','Do you have knowledge of generative AI','Do you have experience with generative AI','Have you used generative AI in a project','Do you have knowledge of Gen AI'],
 'ai_development_duration': ['How much experience do you have with AI Development'],
 'dsa_self_rating': ['Rate your data structures and algorithms skills from 1 to 10','Data structures and algorithms rating','DSA rating from 1 to 10','Rate your data structures and algorithms skills on a scale of 1 to 10'],
 'react_rest_api_experience': ['Have you developed applications using React and backend REST APIs','Have you developed applications using React with backend REST APIs','Have you built React applications with backend REST APIs'],
 'notice_period_days': ['Notice period','Notice period in days','Notice period days','What is your notice period in days','Please enter your notice period in days','Official notice period in days'],
 'serving_notice': ['Are you currently serving notice','Are you currently serving your notice period','Are you serving your notice period','Currently serving notice period'],
 'joining_date': ['Earliest joining date','Earliest start date','Available start date','When can you join','How soon can you join us','If selected how soon could you join mention a date','When are you available to start','What is your current availability to start'],
 'current_ctc': ['Current CTC','Current annual CTC','Current salary','Current annual salary','Present CTC','Please enter your current CTC in INR','Current CTC in LPA','Current annual compensation','What is your C.CTC','C.CTC'],
 'desired_salary': ['Expected CTC','Expected annual CTC','Expected salary','Desired salary','Expected annual salary','Please enter your expected CTC in INR','Expected CTC in LPA','Desired annual compensation'],
 'relocation_within_india': ['Are you willing to relocate within India','Willing to relocate within India','Can you relocate within India','Are you open to relocating anywhere in India','Are you currently based in or willing to work from Gurugram'],
 'onsite_india': ['Are you comfortable working in an onsite setting','Are you comfortable working on-site in India','Are you willing to work on-site in India'],
 'short_profile_summary': ['Summary','Professional summary','Profile summary','Brief professional summary','Short professional summary'],
 'technical_skill_summary': ['Skill set','Technical skill set','Technical skills','Core technical skills','Primary skills'],
 'ai_feature_description': ['Describe a specific AI-powered feature you built','Describe a specific AI-powered feature you built Max 300 characters','Describe an AI feature you built','Give an example of an AI feature you built'],
 'achievement_description': ['Share the most impressive thing that you have built or achieved','What is the most impressive thing you have built','Describe a project you are proud of','What is your most significant project achievement'],
}

# Project/skill presence is distinct from professional duration. These exact
# wordings make no claim about how many years a technology was used.
PROJECT_SKILLS = {
    'javascript': 'JavaScript', 'typescript': 'TypeScript', 'react': 'React',
    'nextjs': 'Next.js', 'nodejs': 'Node.js', 'expressjs': 'Express.js',
    'sql': 'SQL', 'postgresql': 'PostgreSQL', 'mongodb': 'MongoDB',
    'redis': 'Redis', 'tailwind': 'Tailwind CSS', 'git': 'Git', 'docker': 'Docker',
    'github_actions': 'GitHub Actions', 'gcp': 'GCP', 'vercel': 'Vercel',
    'playwright': 'Playwright', 'vitest': 'Vitest', 'sentry': 'Sentry',
    'oauth': 'OAuth', 'python': 'Python', 'django': 'Django', 'livekit': 'LiveKit',
    'webrtc': 'WebRTC', 'stripe': 'Stripe', 'shopify': 'Shopify',
    'woocommerce': 'WooCommerce', 'gohighlevel': 'GoHighLevel', 'clerk': 'Clerk',
    'rest_apis': 'REST APIs', 'webhooks': 'webhooks', 'prisma': 'Prisma',
    'drizzle': 'Drizzle ORM', 'supabase': 'Supabase',
}
for _key, _skill in PROJECT_SKILLS.items():
    ALIASES['skill_' + _key + '_used'] = [
        f'Have you used {_skill}?',
        f'Have you used {_skill} in a project?',
        f'Do you have experience using {_skill}?',
        f'Have you had experience with {_skill}?',
    ]
