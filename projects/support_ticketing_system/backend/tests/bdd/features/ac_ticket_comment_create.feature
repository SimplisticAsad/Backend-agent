# ac.ticket_comment.create  (requirements: requirement.ticket_comments; operations: operation.ticket_comment.create)
Feature: Create Comment succeeds

  Scenario: Create Comment succeeds
    Given the user is signed in as Customer or Agent
    When Fill in the Comment form
    And Create Comment
    Then The Comment is created and appears in the Comment list
