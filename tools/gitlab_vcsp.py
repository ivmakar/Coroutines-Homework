# gitlab_vcsp.py
import os
import re
import logging
from datetime import datetime
import gitlab
from gitlab.exceptions import GitlabGetError, GitlabCreateError
from vcsp_interface import PR, Commit, PRFile, VCSPInterface
from collections import defaultdict

logger = logging.getLogger(__name__)


class GitlabVCSP(VCSPInterface):
    def __init__(self):
        token = os.getenv("GITLAB_TOKEN")
        if not token:
            raise ValueError("GITLAB_TOKEN environment variable is required")

        self.client = gitlab.Gitlab("https://gitlab.com", private_token=token)

    def get_repository(self, repo_name: str):
        try:
            return self.client.projects.get(repo_name)
        except GitlabGetError as e:
            raise Exception(f"Failed to get GitLab repository {repo_name}: {str(e)}")

    def get_pull_request(self, repo_name: str, pr_number: int):
        try:
            project = self.client.projects.get(repo_name)
            mr = project.mergerequests.get(pr_number)
            return PR(
                title=mr.title,
                body=mr.description,
                head_sha=mr.sha,
                state='open'  # there is no MR state in Gitlab api lib
            )
        except GitlabGetError as e:
            raise Exception(f"Failed to get GitLab MR {pr_number} in {repo_name}: {str(e)}")

    def get_last_ai_review_time(self, repo_name: str, pr_number: int):
        """Get the timestamp of the last AI review comment."""
        try:
            project = self.client.projects.get(repo_name)
            mr = project.mergerequests.get(pr_number)
            last_time = None
            
            # Check discussions (inline comments)
            for discussion in mr.discussions.list():
                for note in discussion.attributes.get('notes', []):
                    if "AI Comment:" in note.get('body', ''):
                        note_time = datetime.fromisoformat(note['created_at'].replace('Z', '+00:00'))
                        if not last_time or note_time > last_time:
                            last_time = note_time
            
            # Check notes (general comments)
            for note in mr.notes.list():
                if "AI Comment:" in note.body:
                    note_time = datetime.fromisoformat(note.created_at.replace('Z', '+00:00'))
                    if not last_time or note_time > last_time:
                        last_time = note_time
            
            return last_time
        except (GitlabGetError, ValueError) as e:
            logger.error(f"Failed to get last AI review time: {str(e)}")
            return None

    def get_commits_after_time(self, repo_name: str, pr_number: int, since_time):
        """Get commits in MR after the given time."""
        try:
            project = self.client.projects.get(repo_name)
            mr = project.mergerequests.get(pr_number)
            commits = []
            
            for commit in mr.commits():
                commit_time = datetime.fromisoformat(commit.authored_date.replace('Z', '+00:00'))
                if commit_time > since_time:
                    commits.append({
                        "sha": commit.id,
                        "date": commit.authored_date
                    })
            
            return commits
        except (GitlabGetError, ValueError) as e:
            logger.error(f"Failed to get commits after time: {str(e)}")
            return []

    def get_commit_diff(self, repo_name: str, commit_hash: str):
        """Get diff for a specific commit."""
        try:
            project = self.client.projects.get(repo_name)
            commit = project.commits.get(commit_hash)
            diff_data = commit.diff()
            
            files = []
            for file_diff in diff_data:
                filename = file_diff.get('new_path') or file_diff.get('old_path', '')
                diff_text = file_diff.get('diff', '')
                if diff_text:
                    # Extract changed lines from diff
                    changed_lines = set()
                    line_num_new = None
                    for line in diff_text.splitlines():
                        if line.startswith('@@'):
                            try:
                                match = re.search(r'\+(\d+)', line)
                                if match:
                                    line_num_new = int(match.group(1)) - 1
                            except Exception:
                                pass
                        elif line.startswith('+') and not line.startswith('+++'):
                            if line_num_new is not None:
                                line_num_new += 1
                                changed_lines.add(line_num_new)
                        elif not line.startswith('-'):
                            if line_num_new is not None:
                                line_num_new += 1
                    files.append(PRFile(filename, diff_text, changed_lines))
            
            return files
        except (GitlabGetError, ValueError) as e:
            logger.error(f"Failed to get commit diff for {commit_hash}: {str(e)}")
            return []

    def get_pr_diff(self, repo_name: str, pr_number: int):
        """Get full MR diff."""
        try:
            project = self.client.projects.get(repo_name)
            mr = project.mergerequests.get(pr_number)
            changes = mr.changes()['changes']
            
            files = []
            for change in changes:
                filename = change.get('new_path') or change.get('old_path', '')
                diff_text = change.get('diff', '')
                # Extract changed lines from diff
                changed_lines = set()
                if diff_text:
                    line_num_new = None
                    for line in diff_text.splitlines():
                        if line.startswith('@@'):
                            try:
                                match = re.search(r'\+(\d+)', line)
                                if match:
                                    line_num_new = int(match.group(1)) - 1
                            except Exception:
                                pass
                        elif line.startswith('+') and not line.startswith('+++'):
                            if line_num_new is not None:
                                line_num_new += 1
                                changed_lines.add(line_num_new)
                        elif not line.startswith('-'):
                            if line_num_new is not None:
                                line_num_new += 1
                files.append(PRFile(filename, diff_text, changed_lines))
            
            return files
        except GitlabGetError as e:
            logger.error(f"Failed to get PR diff: {str(e)}")
            return []

    def get_files_in_pr(self, repo_name: str, pr_number: int):
        """Get files in MR, but only new changes if there was a previous AI review."""
        try:
            last_review_time = self.get_last_ai_review_time(repo_name, pr_number)

            if last_review_time:
                commits = self.get_commits_after_time(repo_name, pr_number, last_review_time)
                if not commits:
                    logger.info("No new commits after last AI review.")
                    return []

                per_commit_diffs = []
                for commit in commits:
                    commit_diff = self.get_commit_diff(repo_name, commit["sha"])
                    per_commit_diffs.append(commit_diff)

                # Conflict detection - merge diffs for same files
                merged = defaultdict(lambda: {"diff": [], "lines": set()})
                conflict_files = set()

                for commit_diff in per_commit_diffs:
                    for pr_file in commit_diff:
                        if pr_file.filename in merged:
                            if merged[pr_file.filename]["lines"] & pr_file.lines:
                                conflict_files.add(pr_file.filename)
                        merged[pr_file.filename]["diff"].append(pr_file)
                        if pr_file.lines:
                            merged[pr_file.filename]["lines"].update(pr_file.lines)

                # Final output
                final_files = []
                full_pr_diff = None  # lazy load
                for filename, group in merged.items():
                    if filename in conflict_files:
                        # Use full MR diff for conflicting files
                        if full_pr_diff is None:
                            full_pr_diff = {f.filename: f for f in self.get_pr_diff(repo_name, pr_number)}
                        if filename in full_pr_diff:
                            final_files.append(full_pr_diff[filename])
                        else:
                            logger.warning("Conflict file %s not found in MR diff", filename)
                    else:
                        # Merge patches from multiple commits for same file
                        patches = [f.patch for f in group["diff"] if f.patch]
                        if patches:
                            # Combine patches (simple concatenation, could be improved)
                            combined_patch = "\n".join(patches)
                            final_files.append(PRFile(filename, combined_patch, group["lines"]))

                return final_files
            else:
                # No AI review, return full MR diff
                logger.info("No previous AI comment found, taking full MR diff.")
                return self.get_pr_diff(repo_name, pr_number)
        except GitlabGetError as e:
            raise Exception(f"Failed to get files in GitLab MR {pr_number}: {str(e)}")

    def get_file_content(self, repo_name: str, file_path: str, ref: str = None) -> str:
        try:
            project = self.client.projects.get(repo_name)
            ref = ref or 'main'
            file = project.files.get(file_path=file_path, ref=ref)
            content_bytes = file.decode()
            if not content_bytes:
                raise ValueError(f"File content is empty or not decodable for {file_path}")
            return content_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"Failed to decode file content for {file_path} (possibly binary): {str(e)}")
        except GitlabGetError as e:
            raise Exception(f"Failed to get file content for {file_path} in {repo_name}: {str(e)}")

    def create_issue_comment(self, repo_name: str, pr_number: int, comment: str):
        """Create a general comment on a merge request."""
        try:
            project = self.client.projects.get(repo_name)
            mr = project.mergerequests.get(pr_number)
            mr.notes.create({'body': comment})
            return True
        except GitlabCreateError as e:
            raise Exception(f"Failed to create GitLab issue comment: {str(e)}")

    def create_review_comment(self, repo_name: str, commit: str, file_path: str, line: int, comment: str, side: str):
        try:
            project = self.client.projects.get(repo_name)
            # Find the merge request associated with the commit
            mrs = project.commits.get(commit).merge_requests()
            if not mrs:
                raise Exception(f"No merge request found for commit {commit}")
            mr_id = mrs[0]['iid']  # Get the ID of the first merge request
            mr = project.mergerequests.get(mr_id)  # Fetch the merge request object
            # Create a discussion with a position-based comment
            mr.discussions.create({
                'body': comment,
                'position': {
                    'base_sha': mr.diff_refs['base_sha'],
                    'start_sha': mr.diff_refs['start_sha'],
                    'head_sha': mr.diff_refs['head_sha'],
                    'position_type': 'text',
                    'new_path': file_path,
                    'new_line': line
                }
            })

            return True
        except GitlabCreateError as e:
            raise Exception(f"Failed to create GitLab review comment: {str(e)}")

    def get_commit(self, repo_name: str, commit_sha: str):
        """Retrieve a commit by its SHA from a GitLab repository."""
        try:
            project = self.client.projects.get(repo_name)
            commit = project.commits.get(commit_sha)
            return Commit(
                sha=commit.id,
                message=commit.message,
                author=commit.author_name,
                date=commit.authored_date
            )
        except GitlabGetError as e:
            raise Exception(f"Failed to get GitLab commit {commit_sha} in {repo_name}: {str(e)}")
